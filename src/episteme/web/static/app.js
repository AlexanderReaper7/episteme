/* Single persistent front-end controller.

   The whole app is htmx-boosted (base.html): navigation swaps only the
   #main-content (or admin #admin-main) region and pushes the URL - the page is
   never fully reloaded. So per-page initialization can't live in per-page
   <script> tags; instead this file runs once and re-initializes on every
   `htmx:load` (which htmx fires for the initial document AND for every swapped-in
   fragment). One code path therefore serves both full loads and boosted swaps:

     - narration  (was narrate.js): streaming playback + continuous "podcast" mode
     - rich hydration (was post.js): quiz / chart / diagram, vendors lazy-loaded
     - /tune weight sliders: live readout, dirty tracking, local revert
     - admin nav sync: highlight the active sidebar link (the sidebar persists
       across intra-admin swaps, so the server can't restamp it)

   Everything here is idempotent per element (data-* guards), because htmx may fire
   `htmx:load` more than once over overlapping subtrees. */
(function () {
  "use strict";

  /* ===================== scroll on navigation ========================== */

  // htmx boosts every nav to swap only #main-content / #admin-main. Its default
  // `scrollIntoViewOnBoost` scrolls that target into view after the swap; because
  // .site-header is position:sticky at top:0, aligning the target's top to the
  // viewport top pushes the page down by the header's height - a spurious downward
  // nudge on every nav, even from a fully-scrolled-up page. Disable it and instead
  // scroll the window to the true top on real navigations only. htmx:pushedIntoHistory
  // fires exactly when a URL is pushed (boosted <a>/<form> navs) and NOT for the
  // hx-post mutation buttons (which deliberately don't push URL), so toggles/pins
  // keep their scroll position.
  if (window.htmx && window.htmx.config) {
    window.htmx.config.scrollIntoViewOnBoost = false;
  }
  document.body.addEventListener("htmx:pushedIntoHistory", function () {
    window.scrollTo(0, 0);
  });

  /* ===================== error responses =============================== */

  // htmx does not swap a 4xx/5xx body, so a boosted navigation to a failing page
  // is a click that visibly does nothing at all - the error page would exist and
  // never be seen. The server marks the responses that ARE a rendered error page
  // with `HX-Error-Page` (web/errors.py), and only for a request that targeted a
  // navigation outlet; a failing background poll carries no such header and keeps
  // htmx's default of leaving the document alone. Opting in per response rather
  // than through a global `htmx.config.responseHandling` rule is what keeps those
  // two apart. `isError` stays true so the console still records the failure.
  document.body.addEventListener("htmx:beforeSwap", function (e) {
    var xhr = e.detail.xhr;
    if (xhr && xhr.status >= 400 && xhr.getResponseHeader("HX-Error-Page")) {
      e.detail.shouldSwap = true;
    }
  });

  /* ===================== Glance blocks ================================== */

  // A Glance block that does not answer has to SAY so (0046). htmx leaves a
  // 4xx/5xx body unswapped and these fragments carry no `HX-Error-Page` header
  // (they are not a navigation), so without this the block would sit on
  // "loading" forever and read as a correspondent with nothing this week - which
  // is exactly the state it must not be confusable with.
  //
  // Three events, because three different things go wrong: the correspondent
  // answered with a status (responseError), the request never arrived
  // (sendError), or it outlived the block's own `hx-request` timeout (timeout).
  ["htmx:responseError", "htmx:sendError", "htmx:timeout"].forEach(function (name) {
    document.body.addEventListener(name, function (e) {
      var elt = e.detail && e.detail.elt;
      var block = elt && elt.closest ? elt.closest(".glance-block") : null;
      if (!block) return;
      block.classList.add("glance-block--failed");
      var failed = block.querySelector(".glance-block-failed");
      if (failed) failed.hidden = false;
      var body = block.querySelector(".glance-block-body");
      if (body) body.hidden = true;
    });
  });

  // Delegated: the retry line is in the document from the start, but the block it
  // belongs to may be re-rendered by a swap at any point. The control is a
  // <button>, not a link - every anchor on the page is boosted, and a boosted
  // `href="#"` fires its own request and swaps #main-content out from under the
  // blocks that did load. preventDefault does not stop that; htmx has already
  // taken the click.
  document.addEventListener("click", function (e) {
    var retry = e.target.closest ? e.target.closest("[data-glance-retry]") : null;
    if (!retry) return;
    var block = retry.closest(".glance-block");
    var body = block.querySelector(".glance-block-body");
    block.classList.remove("glance-block--failed");
    retry.closest(".glance-block-failed").hidden = true;
    body.hidden = false;
    body.innerHTML = '<span class="loading">loading…</span>';
    // Re-fires this one fragment through its `retry` trigger. Reloading the page
    // would re-fetch every block, including the ones that answered.
    if (window.htmx) window.htmx.trigger(body, "retry");
  });

  // Search `root` INCLUDING itself - htmx:load hands us the swapped element, which
  // may be the match or an ancestor of it.
  function pick(root, sel) {
    if (root.matches && root.matches(sel)) return root;
    return root.querySelector ? root.querySelector(sel) : null;
  }
  function pickAll(root, sel) {
    var out = [];
    if (root.matches && root.matches(sel)) out.push(root);
    if (root.querySelectorAll) {
      root.querySelectorAll(sel).forEach(function (n) { out.push(n); });
    }
    return out;
  }

  // Resolve a vendor path to its fingerprinted URL (base.html injects the map) so
  // lazily-loaded renderers stay immutable-cacheable; fall back to the plain path.
  function assetUrl(path) {
    var map = window.EPISTEME_ASSETS || {};
    return map[path] || "/static/" + path;
  }

  function loadScript(src) {
    return new Promise(function (resolve, reject) {
      if (document.querySelector('script[src="' + src + '"]')) return resolve();
      var s = document.createElement("script");
      s.src = src;
      s.onload = resolve;
      s.onerror = reject;
      document.head.appendChild(s);
    });
  }

  /* ===================== rich section hydration ========================= */

  function fresh(el) {
    if (el.dataset.hydrated) return false;
    el.dataset.hydrated = "1";
    return true;
  }

  /* Reorder one question's choice buttons in place (Fisher-Yates over the DOM).

     Models put the correct choice at a strongly non-uniform position, so a fixed
     order lets a reader score without reading the question. The shuffle lives here
     rather than in the template because the post page is served with an ETag hashing
     the stored sections: a server-rendered shuffle was discarded by the first 304
     (and by stale-while-revalidate), so a re-read got the identical order. Per page
     VIEW, it survives caching and reloads and a re-read is a real re-test.

     Each button keeps the `data-index` it was rendered with, so `data-answer` stays
     valid under any permutation and nothing is recomputed. */
  function shuffleChoices(item) {
    var box = item.querySelector(".quiz-choices");
    if (!box) return;
    var buttons = Array.prototype.slice.call(box.children);
    for (var i = buttons.length - 1; i > 0; i--) {
      var j = Math.floor(Math.random() * (i + 1));
      var swap = buttons[i];
      buttons[i] = buttons[j];
      buttons[j] = swap;
    }
    buttons.forEach(function (btn) {
      box.appendChild(btn);
    });
  }

  // Hydrate quiz/chart/diagram sections under `root`. Assumes any vendors it needs
  // (vega*, mermaid) are already loaded - hydrateRich guarantees that. Everything
  // degrades: a failed render collapses to its caption.
  function hydrateSections(root) {
    root.querySelectorAll(".section-quiz").forEach(function (quiz) {
      if (!fresh(quiz)) return;
      // Each question resolves on its own and nothing is tallied across them: the
      // check reports what landed, it does not grade the reader (spec §1).
      quiz.querySelectorAll(".quiz-item").forEach(function (item) {
        var answer = parseInt(item.dataset.answer, 10);
        // Scoped to the item, not the section: sibling questions stay live.
        var choices = item.querySelectorAll(".quiz-choice");
        // A question with no valid answer index stays inert rather than scoring
        // every click as wrong. The schema validates answer_index against choices,
        // so nothing the pipeline writes lands here - this guards hand-edited rows.
        if (!(answer >= 0 && answer < choices.length)) return;
        shuffleChoices(item);
        choices.forEach(function (btn) {
          btn.addEventListener("click", function () {
            if (item.classList.contains("quiz-answered")) return;
            item.classList.add("quiz-answered");
            var picked = parseInt(btn.dataset.index, 10);
            btn.classList.add(picked === answer ? "quiz-correct" : "quiz-wrong");
            choices.forEach(function (other) {
              other.disabled = true;
              if (parseInt(other.dataset.index, 10) === answer) {
                other.classList.add("quiz-correct");
              }
            });
            var explanation = item.querySelector(".quiz-explanation");
            if (explanation) explanation.hidden = false;
          });
        });
      });
    });

    if (window.vegaEmbed) {
      root.querySelectorAll(".section-chart[data-spec]").forEach(function (figure) {
        if (!fresh(figure)) return;
        var spec;
        try {
          spec = JSON.parse(figure.dataset.spec);
        } catch (err) {
          figure.classList.add("render-failed");
          return;
        }
        // Vega-Lite's default plot width is a fixed 200px, which strands a chart in
        // about a third of the article column. Fit it to the column instead.
        // Deliberately a measured NUMBER, not width:"container": container sizing
        // reads the target's offsetWidth, which measured 0 here and collapses the
        // chart to nothing - a worse failure than the one being fixed. A 0 reading
        // (hidden section, detached node) falls through to Vega's own default.
        var available = Math.floor(figure.clientWidth);
        if (spec.width === undefined && available > 0) {
          spec.width = Math.max(200, Math.min(available, 900));
          spec.autosize = spec.autosize || { type: "fit", contains: "padding" };
        }
        window
          .vegaEmbed(figure.querySelector(".chart-target"), spec, {
            // Pinned, not inferred: vega-embed picks its parser from the spec's
            // $schema, and the schema the writer generates against carries none.
            mode: "vega-lite",
            actions: false,
            theme: "dark",
            config: { background: "transparent" },
          })
          .catch(function () { figure.classList.add("render-failed"); });
      });
    }

    if (window.mermaid) {
      window.mermaid.initialize({
        startOnLoad: false, theme: "dark", darkMode: true, securityLevel: "strict",
      });
      var pending = [];
      root.querySelectorAll(".section-diagram pre.mermaid").forEach(function (pre) {
        if (fresh(pre)) pending.push(pre);
      });
      if (pending.length) {
        window.mermaid.run({ nodes: pending }).catch(function () {
          pending.forEach(function (pre) {
            if (!pre.querySelector("svg")) {
              pre.closest(".section-diagram").classList.add("render-failed");
            }
          });
        });
      }
    }
  }

  // Lazy-load only the vendors the present sections need (in required order), then
  // hydrate. Charts/diagrams pull their heavy renderers; a quiz needs none.
  function hydrateRich(root) {
    root = root || document;
    var hasInteractive = pick(root, ".section-quiz, .section-chart, .section-diagram");
    if (!hasInteractive) return;
    var need = [];
    if (pick(root, ".section-chart")) {
      need.push(
        assetUrl("vendor/vega.min.js"),
        assetUrl("vendor/vega-lite.min.js"),
        assetUrl("vendor/vega-embed.min.js")
      );
    }
    if (pick(root, ".section-diagram")) {
      need.push(assetUrl("vendor/mermaid.min.js"));
    }
    var seq = Promise.resolve();
    need.forEach(function (src) {
      seq = seq.then(function () { return loadScript(src); });
    });
    seq
      .then(function () { hydrateSections(root.querySelectorAll ? root : document); })
      .catch(function () {});
  }

  /* ========================== narration ================================= */

  // Initialize the streaming <audio> + continuous mode for one .post-audio root.
  // Guarded so repeated htmx:load events (and the continuous-advance swap, which
  // reuses the same live element) never double-bind.
  function initNarration(root) {
    if (root.dataset.narrateInit) return;
    root.dataset.narrateInit = "1";

    var postId = root.dataset.postId;
    var select = root.querySelector(".voice-select");
    var player = root.querySelector(".post-audio-player");
    var statusEl = root.querySelector(".voice-status");
    var continuous = root.querySelector(".continuous-toggle");
    if (!player || !select || !continuous || !statusEl) return;

    var CONT_KEY = "episteme:narrate:continuous";
    var VOICE_KEY = "episteme:narrate:voice";
    var params = new URLSearchParams(window.location.search);

    function setStatus(msg) { statusEl.textContent = msg || ""; }
    function voice() { return select ? select.value : ""; }
    function selectVoice(v) {
      if (!v) return;
      for (var i = 0; i < select.options.length; i++) {
        if (select.options[i].value === v) { select.value = v; return; }
      }
    }
    function loadSource() {
      // preload="none" keeps this from fetching until play() / the controls fire.
      player.src =
        "/api/posts/" + postId + "/audio/stream?voice=" + encodeURIComponent(voice());
    }

    try {
      if (localStorage.getItem(CONT_KEY) === "1") continuous.checked = true;
      selectVoice(localStorage.getItem(VOICE_KEY));
    } catch (e) {}

    continuous.addEventListener("change", function () {
      try { localStorage.setItem(CONT_KEY, continuous.checked ? "1" : "0"); } catch (e) {}
    });

    select.addEventListener("change", function () {
      try { localStorage.setItem(VOICE_KEY, voice()); } catch (e) {}
      var wasPlaying = !player.paused && !player.ended;
      loadSource();
      setStatus("");
      if (wasPlaying) player.play().catch(function () {});
    });

    player.addEventListener("playing", function () { setStatus(""); });
    player.addEventListener("waiting", function () { setStatus("Buffering…"); });
    player.addEventListener("error", function () {
      setStatus("Narration failed - try again.");
    });
    player.addEventListener("ended", function () {
      if (continuous.checked) advance();
    });

    // Warm the next post while the current narration is still playing, so the
    // continuous ("podcast") handoff has no fetch gap. Resolve its id and pull its
    // HTML (a plain GET, so it shares the /post/{id} ETag and the browser cache) as
    // playback nears the end; advance() reuses it. Guarded to run at most once per
    // post; the prefetch is discarded when we actually move on (see swapTo).
    var prefetch = null;      // {id, html} once warmed
    var prefetching = false;  // a warm is in flight
    player.addEventListener("timeupdate", function () {
      if (!continuous.checked || prefetching || prefetch) return;
      var dur = player.duration;
      if (!dur || isNaN(dur) || !isFinite(dur)) return;
      if (dur - player.currentTime > 15) return;
      prefetching = true;
      fetch("/api/posts/" + postId + "/next")
        .then(function (r) { return r.json(); })
        .then(function (d) {
          if (!d || !d.next_id) return;
          return fetch("/post/" + d.next_id)
            .then(function (r) { return r.text(); })
            .then(function (html) { prefetch = { id: d.next_id, html: html }; });
        })
        .catch(function () {})
        .then(function () { prefetching = false; });
    });

    function hardNavigate(nextId) {
      window.location.href =
        "/post/" + nextId +
        "?autoplay=1&continuous=1&voice=" + encodeURIComponent(voice());
    }

    // Continuous ("podcast") advance: fetch the next post and swap it in *around*
    // the still-playing <audio>, keeping that element alive. A media element that
    // already played from a user gesture may be controlled programmatically after,
    // so playback continues without the fresh-load autoplay block a real navigation
    // would hit. This is the one place we bypass htmx's boosted swap on purpose. The
    // URL is updated with replaceState (not pushState) so Back returns to the feed
    // htmx already has cached, rather than stranding a synthetic history entry.
    function swapTo(nextId, prefetchedHtml) {
      var htmlPromise = prefetchedHtml
        ? Promise.resolve(prefetchedHtml)
        : fetch("/post/" + nextId).then(function (r) { return r.text(); });
      return htmlPromise
        .then(function (html) {
          var doc = new DOMParser().parseFromString(html, "text/html");
          var newArticle = doc.querySelector(".article-page");
          var curArticle = document.querySelector(".article-page");
          var placeholder = newArticle && newArticle.querySelector(".post-audio");
          if (!newArticle || !curArticle || !placeholder) { hardNavigate(nextId); return; }
          // Move the live player into the incoming article, then swap the article in.
          // Both mutations are synchronous, so the element is never detached long
          // enough for the browser to pause it.
          placeholder.parentNode.replaceChild(root, placeholder);
          curArticle.parentNode.replaceChild(newArticle, curArticle);

          document.title = doc.title;
          history.replaceState(null, "", "/post/" + nextId);
          postId = String(nextId);
          root.dataset.postId = postId;
          // New current post: any warm for the OLD next is now consumed/stale.
          prefetch = null;
          prefetching = false;

          loadSource();
          setStatus("");
          player.play().catch(function () { setStatus("Press play to continue."); });
          hydrateRich(newArticle);
        })
        .catch(function () { hardNavigate(nextId); });
    }

    function advance() {
      setStatus("Loading next…");
      // Use the warmed next post if we have one (no fetch gap); else resolve it now.
      if (prefetch && prefetch.id) {
        var warm = prefetch;
        prefetch = null;
        return swapTo(warm.id, warm.html);
      }
      fetch("/api/posts/" + postId + "/next")
        .then(function (r) { return r.json(); })
        .then(function (d) {
          if (d && d.next_id) return swapTo(d.next_id);
          setStatus("End of feed.");
        })
        .catch(function () { setStatus(""); });
    }

    // ?continuous=1 / ?voice= / ?autoplay=1 - the hard-navigation fallback landing.
    if (params.get("continuous") === "1") {
      continuous.checked = true;
      try { localStorage.setItem(CONT_KEY, "1"); } catch (e) {}
    }
    selectVoice(params.get("voice"));
    loadSource();
    if (params.get("autoplay") === "1") {
      player.play().catch(function () { setStatus("Press play to continue."); });
    }
  }

  /* ===================== /tune weight sliders ============================ */

  // The weight list is one form committed by an explicit Save: dragging several
  // sliders is a single editing session, so nothing posts until the reader says so.
  //
  //   * the live readout and the centre-anchored fill while dragging;
  //   * `dirty` - the slugs actually moved, and the ONLY thing the server records
  //     from. This is the load-bearing part: only the client knows which sliders
  //     moved, because only it holds each one's rendered starting value. A server
  //     comparing submitted values against the stored profile would record every
  //     slider whose weight had decayed past its own rounding since the page
  //     rendered - pinning weights nobody touched. So the list genuinely needs
  //     JavaScript (the panel says so in a <noscript>, and the "set a topic by
  //     name" box reaches every one of these topics without it).
  //   * revert, which is purely local: the server was never told.
  function initWeights(form) {
    if (form.dataset.weightsInit) return;
    form.dataset.weightsInit = "1";

    var clamp = 1;
    var first = form.querySelector(".weight-slider");
    if (first) clamp = Math.abs(parseFloat(first.max)) || 1;
    var dirtyField = form.querySelector("[data-dirty]");
    var save = form.querySelector(".weight-save");
    var revert = form.querySelector("[data-revert]");
    var pending = form.querySelector("[data-pending]");

    function paint(slider) {
      var value = parseFloat(slider.value);
      var offset = (Math.abs(value) / clamp) * 50;
      slider.style.setProperty("--fill-from", (value >= 0 ? 50 : 50 - offset) + "%");
      slider.style.setProperty("--fill-to", (value >= 0 ? 50 + offset : 50) + "%");
      slider.style.setProperty(
        "--fill-color",
        value >= 0 ? "var(--accent)" : "var(--bad-muted)"
      );
      var out = form.querySelector('output[for="' + slider.id + '"]');
      if (out) out.textContent = (value > 0 ? "+" : "") + value.toFixed(1);
    }

    function refresh() {
      var dirty = [];
      form.querySelectorAll(".weight-slider").forEach(function (slider) {
        // Compared as NUMBERS, not strings: the browser sanitizes a range input's
        // value onto its step grid and drops a trailing zero, so a slider rendered
        // as value="-2.0" reads back "-2" and would look moved on first paint -
        // every whole-numbered weight arriving pre-dirty.
        var moved =
          Math.abs(
            parseFloat(slider.value) - parseFloat(slider.dataset.initial)
          ) > 1e-9;
        slider.closest(".weight-row").classList.toggle("is-dirty", moved);
        if (moved) dirty.push(slider.name.replace(/^w:/, ""));
      });
      if (dirtyField) dirtyField.value = dirty.join(",");
      if (save) save.disabled = dirty.length === 0;
      if (revert) revert.hidden = dirty.length === 0;
      if (pending) {
        pending.textContent = dirty.length
          ? dirty.length + " unsaved change" + (dirty.length === 1 ? "" : "s")
          : "";
      }
    }

    form.addEventListener("input", function (e) {
      if (!e.target.classList.contains("weight-slider")) return;
      paint(e.target);
      refresh();
    });

    if (revert) {
      revert.addEventListener("click", function () {
        form.querySelectorAll(".weight-slider").forEach(function (slider) {
          slider.value = slider.dataset.initial;
          paint(slider);
        });
        refresh();
      });
    }

    refresh();
  }

  /* ========================== admin nav ================================= */

  // The boosted admin sidebar persists across intra-admin swaps, so its active link
  // must be re-derived client-side from the URL after each navigation.
  function syncNav() {
    var path = window.location.pathname;
    document.querySelectorAll(".admin-sidebar-nav a").forEach(function (a) {
      var href = a.getAttribute("href");
      var active = href === "/admin" ? path === "/admin" : path.indexOf(href) === 0;
      a.classList.toggle("active", active);
    });
  }

  /* ================== expanded queue groups (admin) ====================== */

  // The queue folds repeated runs of a task into one row; which folds are open
  // has to survive the 10s poll, or a fold opens under the reader and shuts
  // itself 90 seconds later.
  //
  // The toggle itself is a checkbox revealed by CSS, so the fold works with JS
  // off. This code only RECORDS the open keys into a cookie; the server stamps
  // `checked` when it renders (see admin.open_groups). That ordering is the
  // whole point: re-applying state from script AFTER a swap is what made the
  // panel flash and what moved the scroll position under the cursor - a
  // server-stamped fragment arrives in its final shape with nothing to replay.
  //
  // One cookie holding every open key, not one cookie per group: the key set is
  // task names, small but unbounded, and a cookie each would ride along with
  // every request for the rest of the session.
  var QUEUE_GROUP_COOKIE = "qopen";

  function recordOpenGroups() {
    var keys = [];
    pickAll(document, ".job-group-toggle:checked").forEach(function (el) {
      keys.push(el.value);
    });
    // Session cookie, path-scoped to the admin area: view state, not something
    // worth persisting past the browser session or sending with every request
    // for an article.
    document.cookie =
      QUEUE_GROUP_COOKIE +
      "=" +
      encodeURIComponent(keys.join("|")) +
      ";path=/admin;samesite=lax";
  }

  function initQueueGroups(root) {
    pickAll(root || document, ".job-group-toggle").forEach(function (el) {
      if (!fresh(el)) return;
      el.addEventListener("change", recordOpenGroups);
    });
  }

  /* ===================== vocabulary search =============================== */

  // /admin/topics folds its table shut and filters it here rather than on the
  // server. The vocabulary is a closed set already rendered in full, so a round
  // trip per keystroke would buy nothing; and rename and merge swap `#topic-rows`
  // wholesale, which a server-side query would have to be threaded back through
  // on every edit.
  //
  // The count is owned here for the same reason: a merge deletes a row from that
  // tbody without re-rendering the summary sitting above it, so a server-rendered
  // number goes stale on the first edit. The server still stamps the initial one,
  // which is what a reader without JS sees.
  function applyTopicFilter() {
    var input = document.getElementById("topic-filter");
    var fold = document.getElementById("topic-fold");
    if (!input || !fold) return;
    var query = input.value.trim().toLowerCase();
    var rows = fold.querySelectorAll("tr[data-search]");
    var shown = 0;
    rows.forEach(function (row) {
      var hit = !query || row.dataset.search.indexOf(query) !== -1;
      row.hidden = !hit;
      if (hit) shown++;
    });
    var count = document.getElementById("topic-count");
    if (count) {
      count.textContent = query
        ? shown + " of " + rows.length + " topics"
        : rows.length + " topics";
    }
    var empty = document.getElementById("topic-no-match");
    if (empty) empty.hidden = !(query && shown === 0);
    // Typing opens the fold, since matches inside a shut one are invisible.
    // Clearing the box deliberately does NOT close it again: the reader may have
    // opened it by hand, and a fold that shuts itself under the cursor is worse
    // than one left open.
    if (query) fold.open = true;
  }

  function initTopicSearch(root) {
    var input = pick(root || document, "#topic-filter");
    if (input && fresh(input)) input.addEventListener("input", applyTopicFilter);
    // Unconditionally on every load, not just a fresh input: this also runs after
    // an htmx swap of `#topic-rows`, where the rows are new and the filter that
    // was applied to their predecessors is not.
    applyTopicFilter();
  }

  // A <button> inside a <summary> still runs the summary's activation behaviour,
  // so pressing "run" on a collapsed stage would also expand it - the block would
  // open and close under the cursor on every click. preventDefault on the bubbled
  // click cancels that activation; it does not touch htmx, which has already
  // taken the event, nor a type=button, which has no default of its own.
  //
  // Delegated at document level rather than bound per button: these blocks arrive
  // through htmx swaps, and a listener attached at render time would be gone the
  // first time the section came back from the server.
  document.addEventListener("click", function (e) {
    if (e.target.closest("summary button")) e.preventDefault();
  });

  /* ===================== relative timestamps ============================ */

  // Retime every <time data-ago> from its own `datetime` attribute.
  //
  // This is what lets the polled fragments answer 204 for minutes at a stretch:
  // if the age were baked into the HTML, "2m ago" becoming "3m ago" would change
  // the rendered output - and so the state digest - while nothing had actually
  // happened, forcing a full re-render on a timer. Mirrors templating._ago's
  // thresholds; the pair is duplicated on purpose (one renders, one retimes) and
  // pinned by test_queue_render.py.
  function agoText(seconds) {
    var span = Math.abs(seconds);
    var text;
    if (span < 60) text = Math.round(span) + "s";
    else if (span < 3600) text = Math.round(span / 60) + "m";
    else if (span < 86400) text = Math.round(span / 3600) + "h";
    else text = Math.round(span / 86400) + "d";
    if (seconds < 0) return "in " + text;
    if (seconds < 45) return "just now";
    return text + " ago";
  }

  function retimeAgo(root) {
    var now = Date.now();
    pickAll(root || document, "time[data-ago][datetime]").forEach(function (el) {
      var at = Date.parse(el.getAttribute("datetime"));
      if (!isNaN(at)) el.textContent = agoText((now - at) / 1000);
    });
  }

  // 30s: half the coarsest thing the text can express short of an hour, so a
  // displayed age is never more than one step stale. Costs no network and
  // touches only text nodes, so it neither reflows the table nor scrolls it.
  setInterval(function () { retimeAgo(document); }, 30000);

  /* ==================== llama.cpp log stream (admin) ===================== */

  // The pane is APPEND-ONLY. It used to re-fetch its whole 300-line tail every
  // 3s and swap it in, which discarded the operator's text selection and reset
  // their scrollback on every tick - while they were reading it. The server now
  // pushes only the lines written since the last byte offset (see
  // api.api_llm_logs_stream) and we append them as their own text node; existing
  // nodes are never rewritten, so a selection spanning them survives.
  //
  // Exactly one stream can be open, because there is exactly one pane. Tracking
  // the ELEMENT rather than a boolean is what makes this idempotent under
  // htmx:load, which fires for every swap anywhere on the page: same element,
  // nothing to do; different or gone, close the old one first - an htmx swap
  // discards the node without telling us, and an orphaned EventSource would go
  // on polling llama-warden for a pane nobody can see.
  var LOG_MAX_LINES = 5000;
  var logStream = null;
  var logPane = null;

  function logPart(pane, sel) {
    var box = pane.closest("#backend-log");
    return box ? box.querySelector(sel) : null;
  }

  function appendLog(pane, lines) {
    if (!lines.length) return;
    var node = document.createTextNode(lines.join("\n") + "\n");
    node.lineCount = lines.length;
    pane.appendChild(node);
    var total = Number(pane.dataset.logLines || 0) + lines.length;
    // Drop whole leading nodes rather than re-slicing text: not touching what is
    // already rendered is the entire point of this pane.
    while (total > LOG_MAX_LINES && pane.firstChild && pane.firstChild !== node) {
      total -= pane.firstChild.lineCount || 0;
      pane.removeChild(pane.firstChild);
    }
    pane.dataset.logLines = total;
  }

  function syncLogStream() {
    var pane = document.querySelector("[data-log-stream]");
    if (pane === logPane) return;
    if (logStream) { logStream.close(); logStream = null; }
    logPane = pane;
    if (!pane) return;

    // The server-rendered snapshot is one untracked node: give it its line count
    // and a trailing newline so every later append is uniform.
    if (pane.firstChild) {
      pane.firstChild.lineCount = Number(pane.dataset.logLines || 0);
      if (pane.textContent.slice(-1) !== "\n") pane.appendChild(document.createTextNode("\n"));
    }

    var stream = new EventSource(pane.dataset.logStream);
    logStream = stream;

    function paint(e, replace) {
      if (stream !== logStream) return;  // a swap raced us; this stream is stale
      var data = JSON.parse(e.data);
      if (replace) {
        pane.textContent = "";
        pane.dataset.logLines = 0;
        // A reset after a backlog overrun skipped bytes. Say so, rather than
        // splicing two distant parts of the log into one continuous-looking pane.
        if (data.gap_bytes) {
          appendLog(pane, ["… " + Math.round(data.gap_bytes / 1024) + " KB skipped …"]);
        }
      }
      appendLog(pane, data.lines || []);
      var status = logPart(pane, "[data-log-status]");
      if (status) status.textContent = "";
      var note = logPart(pane, "[data-log-note]");
      if (note && data.exists) note.remove();
      var size = logPart(pane, "[data-log-size]");
      if (size && data.exists) size.textContent = (data.size_bytes / 1024).toFixed(1) + " KB";
    }

    function say(text) {
      if (stream !== logStream) return;
      var status = logPart(pane, "[data-log-status]");
      if (status) status.textContent = text;
    }

    stream.addEventListener("reset", function (e) { paint(e, true); });
    stream.addEventListener("lines", function (e) { paint(e, false); });
    // The agent being down is a normal state - it is optional infrastructure, and
    // watching it restart is a reason to have this pane open - so neither failure
    // closes anything: the server keeps polling through `unavailable`, and
    // EventSource reconnects itself after a transport `error`. Only the status
    // line changes. (Two events, not one: a server-sent `error` would land on the
    // same handler as the transport's own, distinguishable only by `data`.)
    stream.addEventListener("unavailable", function (e) { say(JSON.parse(e.data).detail); });
    stream.addEventListener("error", function () { say("reconnecting…"); });
  }

  /* ================== benchmark progress + form (admin) ================= */

  // Same element-tracking idempotence as the log pane above, and for the same
  // reason: an htmx swap discards the node without telling us, and an orphaned
  // EventSource would keep a worker-side query running for a pane nobody sees.
  var benchStream = null;
  var benchBox = null;

  function benchText(box, sel, text) {
    var node = box.querySelector(sel);
    if (node) node.textContent = text;
  }

  function syncBenchProgress() {
    var box = document.querySelector("[data-bench-progress]");
    if (box === benchBox) return;
    if (benchStream) { benchStream.close(); benchStream = null; }
    benchBox = box;
    if (!box) return;

    var stream = new EventSource(box.dataset.benchProgress);
    benchStream = stream;

    stream.addEventListener("progress", function (e) {
      if (stream !== benchStream) return;
      var d = JSON.parse(e.data);
      var item = (d.item || 0) + 1;
      var label = d.model
        ? item + "/" + d.items + "  " + d.model + (d.variant ? " · " + d.variant : "")
        : d.status;
      benchText(box, "[data-bench-label]", label + (d.cancel_requested ? "  (cancelling)" : ""));
      // Two progress questions, and only one of them has a denominator: prefill
      // knows its total, decode does not (max_tokens is a ceiling, not a plan).
      // So the bar tracks the ITEMS, which always has one, and the phase line
      // carries the within-item detail.
      var pct = d.items ? Math.round((d.item || 0) / d.items * 100) : 0;
      var bar = box.querySelector("[data-bench-bar]");
      if (bar) bar.style.width = pct + "%";
      var detail = d.phase === "prefill"
        ? "prefill " + (d.processed || 0) + " / " + (d.total || "?") + " tokens"
        : d.phase === "decode"
          ? "generating, " + (d.processed || 0) + " tokens"
          : (d.phase || "");
      if (d.elapsed_ms) detail += "  (" + Math.round(d.elapsed_ms / 1000) + "s)";
      benchText(box, "[data-bench-detail]", detail);
    });

    // The run is over: reload so the page renders its charts and its comparison
    // table. A partial swap would have to know which sections exist, and they
    // differ by scenario.
    stream.addEventListener("done", function () {
      if (stream !== benchStream) return;
      stream.close();
      benchStream = null;
      window.location.reload();
    });
  }

  // The scenario-specific fieldsets are all in the DOM and toggled here rather
  // than fetched: the server has nothing new to say when the dropdown changes.
  function initBenchForm(root) {
    pickAll(root, "#bench-scenario").forEach(function (select) {
      function apply() {
        pickAll(document, "[data-bench-when]").forEach(function (box) {
          box.hidden = box.dataset.benchWhen !== select.value;
        });
      }
      select.addEventListener("change", apply);
      apply();
    });
  }

  /* ===================== the assistant rail ============================= */

  /* Reads the turn stream with fetch() + ReadableStream rather than EventSource,
     because the request is a POST with a body. That is also why the rail is not
     htmx: htmx swaps a finished response, and the whole point here is the
     unfinished one.

     The rail lives outside #main-content, so it survives boosted navigation -
     including mid-stream. `currentPostId` therefore has to be re-read from the
     URL on every navigation instead of captured when the panel was built. */

  var chatBusy = false;

  function currentPostId() {
    var m = /^\/post\/(\d+)/.exec(window.location.pathname);
    return m ? m[1] : null;
  }

  function chatLog() { return document.querySelector("[data-chat-log]"); }

  function scrollChat() {
    var log = chatLog();
    if (log) log.scrollTop = log.scrollHeight;
  }

  function chatBubble(role) {
    var log = chatLog();
    if (!log) return null;
    var empty = log.querySelector(".chat-empty");
    if (empty) empty.remove();
    var div = document.createElement("div");
    div.className = "chat-msg chat-msg-" + role + " chat-msg-streaming";
    log.appendChild(div);
    scrollChat();
    return div;
  }

  // Append a text NODE rather than rewriting textContent: a rewrite collapses any
  // selection the reader has made in the scrollback, on every single token.
  function chatAppend(bubble, text) {
    if (!bubble || !text) return;
    bubble.appendChild(document.createTextNode(text));
    scrollChat();
  }

  async function chatFetchInto(url, place) {
    var res = await fetch(url, { headers: { "HX-Request": "true" } });
    if (!res.ok) return;
    var wrap = document.createElement("div");
    wrap.innerHTML = await res.text();
    var node = wrap.firstElementChild;
    if (node) place(node);
    scrollChat();
  }

  // One SSE frame parser for both streams. The event NAME carries the type, so a
  // frame is (event, data) and nothing has to sniff the payload.
  function chatFrames(chunk, onEvent) {
    chunk.split("\n\n").forEach(function (frame) {
      var name = null;
      var data = "";
      frame.split("\n").forEach(function (line) {
        if (line.indexOf("event:") === 0) name = line.slice(6).trim();
        else if (line.indexOf("data:") === 0) data += line.slice(5).trim();
      });
      if (!name) return;
      var parsed = {};
      try { parsed = data ? JSON.parse(data) : {}; } catch (err) { return; }
      onEvent(name, parsed);
    });
  }

  async function chatStream(url, options) {
    if (chatBusy) return;
    chatBusy = true;
    var form = document.querySelector("[data-chat-form]");
    if (form) form.classList.add("is-busy");
    var bubble = null;
    var status = null;

    function note(text) {
      if (!status) {
        status = document.createElement("div");
        status.className = "chat-status";
        var log = chatLog();
        if (log) log.appendChild(status);
      }
      status.textContent = text;
      scrollChat();
    }

    try {
      var res = await fetch(url, options);
      if (!res.ok || !res.body) { note("The assistant is unreachable (" + res.status + ")."); return; }
      var reader = res.body.getReader();
      var decoder = new TextDecoder();
      var buffer = "";
      for (;;) {
        var step = await reader.read();
        if (step.done) break;
        buffer += decoder.decode(step.value, { stream: true });
        // Keep the trailing partial frame; frames are separated by a blank line.
        var cut = buffer.lastIndexOf("\n\n");
        if (cut < 0) continue;
        var whole = buffer.slice(0, cut);
        buffer = buffer.slice(cut + 2);
        chatFrames(whole, function (name, data) {
          if (name === "text") {
            if (!bubble) bubble = chatBubble("assistant");
            if (status) { status.remove(); status = null; }
            chatAppend(bubble, data.delta || "");
          } else if (name === "tool") {
            note("using " + (data.name || "a tool") + "…");
          } else if (name === "saved") {
            // Swap the raw stream for the server's rendered Markdown, so a live
            // answer and a reloaded one are the same HTML.
            var streamed = bubble;
            bubble = null;
            chatFetchInto("/chat/message/" + data.id, function (node) {
              if (streamed) streamed.replaceWith(node); else chatLog().appendChild(node);
            });
          } else if (name === "proposal") {
            chatFetchInto("/chat/proposal/" + data.id, function (node) {
              chatLog().appendChild(node);
            });
          } else if (name === "resolved") {
            var card = document.querySelector('[data-proposal="' + data.id + '"]');
            if (card) card.remove();
          } else if (name === "result") {
            note(data.text || "");
            status = null;  // keep it: it is the outcome, not a transient
          } else if (name === "error") {
            note(data.message || "Something went wrong.");
            status = null;
          }
        });
      }
    } catch (err) {
      note("The connection dropped: " + err);
    } finally {
      chatBusy = false;
      if (form) form.classList.remove("is-busy");
    }
  }

  function initChat(root) {
    var rail = pick(root, "[data-chat]");
    if (!rail || rail.dataset.chatReady) return;
    rail.dataset.chatReady = "1";

    rail.querySelectorAll("[data-chat-toggle]").forEach(function (button) {
      button.addEventListener("click", function () {
        var open = rail.classList.toggle("is-open");
        rail.querySelectorAll("[data-chat-toggle]").forEach(function (b) {
          if (b.hasAttribute("aria-expanded")) b.setAttribute("aria-expanded", String(open));
        });
        if (open) {
          scrollChat();
          var box = rail.querySelector("textarea");
          if (box) box.focus();
        }
      });
    });

    var form = rail.querySelector("[data-chat-form]");
    if (form) {
      form.addEventListener("submit", function (e) {
        e.preventDefault();
        var box = form.querySelector("textarea");
        var text = (box.value || "").trim();
        if (!text || chatBusy) return;
        var bubble = chatBubble("user");
        if (bubble) bubble.textContent = text;
        box.value = "";
        var body = new FormData();
        body.append("text", text);
        var postId = currentPostId();
        chatStream("/chat/turn" + (postId ? "?post_id=" + postId : ""), {
          method: "POST",
          body: body,
        });
      });
      // Enter sends, Shift+Enter is a newline - the convention every chat box uses.
      form.addEventListener("keydown", function (e) {
        if (e.key === "Enter" && !e.shiftKey && e.target.tagName === "TEXTAREA") {
          e.preventDefault();
          form.requestSubmit();
        }
      });
    }

    // Delegated, because approval cards arrive after this runs - both from the
    // live stream and from a re-fetched scrollback.
    rail.addEventListener("click", function (e) {
      var button = e.target.closest ? e.target.closest("[data-chat-resolve]") : null;
      if (!button) return;
      var id = button.dataset.chatResolve;
      var approve = button.dataset.approve;
      button.closest(".chat-proposal").classList.add("is-resolving");
      chatStream("/chat/proposal/" + id + "/resolve?approve=" + approve, { method: "POST" });
    });
  }

  /* ========================= the header menu ============================ */

  // Below 40rem the header nav is a panel behind a burger (0053). The state is
  // one class on the nav, which the media query is the only reader of - above
  // 40rem the links are laid out inline and the class means nothing, so nothing
  // has to be undone when the window grows.
  function setSiteMenu(open) {
    var nav = document.getElementById("site-nav");
    var button = document.querySelector("[data-site-menu]");
    if (!nav || !button) return;
    nav.classList.toggle("is-open", open);
    button.setAttribute("aria-expanded", open ? "true" : "false");
  }

  // One handler for opening and for every way of closing. A click inside the
  // panel closes it because that click IS the navigation - and a boosted link
  // swaps #main-content without touching the header, so nothing else would.
  // Closing on `htmx:load` instead would be wrong for the same reason it looks
  // right: a polling fragment anywhere on the page fires it too.
  document.addEventListener("click", function (e) {
    if (!e.target.closest) return;
    var nav = document.getElementById("site-nav");
    if (!nav) return;
    if (e.target.closest("[data-site-menu]")) {
      setSiteMenu(!nav.classList.contains("is-open"));
      return;
    }
    if (nav.classList.contains("is-open")) setSiteMenu(false);
  });

  document.addEventListener("keydown", function (e) {
    if (e.key !== "Escape") return;
    var nav = document.getElementById("site-nav");
    if (!nav || !nav.classList.contains("is-open")) return;
    setSiteMenu(false);
    // Dismissing with the keyboard has to leave the focus somewhere the reader
    // can see, and the button is where they opened it from.
    var button = document.querySelector("[data-site-menu]");
    if (button) button.focus();
  });

  /* ===================== clamped card summaries ========================= */

  // The gap left under a card that fills the window, so the next one's top edge
  // shows and the feed still reads as a stream rather than a slideshow.
  var CARD_VIEWPORT_GAP = 48;
  // No card shows less than this, however cramped. Below three lines a summary
  // stops being a paragraph and the reader is choosing from titles alone.
  var MIN_SNIPPET_LINES = 3;

  function outerHeight(el) {
    var cs = getComputedStyle(el);
    return (
      el.getBoundingClientRect().height +
      parseFloat(cs.marginTop) +
      parseFloat(cs.marginBottom)
    );
  }

  // How many lines of THIS card's summary there is room for. The clamp used to
  // be the constant 9, measured once at 2560px against the layout of the day,
  // which is a number that is right for one window and wrong for every other:
  // the same card on a phone, with the picture stacked on top of it, wants four.
  //
  // The budget is the viewport, minus the header the card scrolls under. A card
  // taller than the window cannot be read without scrolling past its own rating
  // controls, and its summary cannot be judged against the ones around it. What
  // is left after everything else in the body - meta, title, chips, rating row,
  // padding, and the picture above it when the layout is stacked - is the
  // summary's, and `floor` turns it into whole lines so the cut lands between
  // them instead of through one.
  //
  // Measured from the DOM rather than derived from the CSS: the two layouts
  // (beside, stacked) and every card kind put different things in the body, and
  // an arithmetic copy of that here would be wrong the first time a card grew a
  // row. `.card-summary` is what the space arrives in - it is the flex item that
  // absorbs the card's slack - so its own height is not part of the question.
  function fitSnippet(snippet) {
    var summary = snippet.parentElement;
    if (!summary || !summary.classList.contains("card-summary")) return;
    var body = summary.parentElement;
    if (!body || !body.classList.contains("card-body")) return;
    var card = body.parentElement;
    var lineHeight = parseFloat(getComputedStyle(snippet).lineHeight);
    var cs = getComputedStyle(body);
    var taken = parseFloat(cs.paddingTop) + parseFloat(cs.paddingBottom);
    for (var i = 0; i < body.children.length; i++) {
      if (body.children[i] !== summary) taken += outerHeight(body.children[i]);
    }
    for (var j = 0; j < summary.children.length; j++) {
      var part = summary.children[j];
      if (part === snippet) continue;
      // The expand button is `display: none` until the text is known to
      // overflow, so on a card that does not overflow yet it measures zero and
      // the space it would need is missing from the sum. One line is what it
      // takes; reserving it always costs an unclamped card nothing, because it
      // has spare room by definition.
      taken += Math.max(outerHeight(part), lineHeight);
    }
    // Stacked layouts put the picture inside the card's height rather than
    // beside it, and it is not in the body - so ask the card, not the body.
    var banner = card ? card.querySelector(".card-banner") : null;
    if (banner && banner.getBoundingClientRect().bottom <= body.getBoundingClientRect().top + 1) {
      taken += outerHeight(banner);
    }
    var header = document.querySelector(".site-header");
    var budget =
      window.innerHeight -
      (header ? header.getBoundingClientRect().height : 0) -
      CARD_VIEWPORT_GAP;
    var lines = Math.floor((budget - taken) / lineHeight);
    snippet.style.setProperty(
      "--card-snippet-lines",
      String(Math.max(MIN_SNIPPET_LINES, lines))
    );
  }

  // A feed card's summary shows as much as the card has room for and fades out
  // when there is more (0050). No CSS selector can ask "did this overflow", and
  // a mask applied unconditionally greys the last line of every summary that
  // fit - which is most of them, since summaries are written to fit. So the
  // question is asked here, where the answer exists: scrollHeight is the
  // unclamped height, clientHeight the clamped box.
  //
  // Re-measured on every htmx:load rather than once: the feed's infinite scroll
  // swaps in new cards, and a font that loads late changes every answer. The
  // class is removed before measuring so a card that stops overflowing (a
  // narrower window, a bigger viewport) loses its fade.
  function markClampedText(root) {
    pickAll(root, ".card-snippet").forEach(function (el) {
      // An expanded snippet has no clamp, so scrollHeight and clientHeight agree
      // and the measurement would answer "it fits" - taking away the button that
      // closes it. Nothing to measure while it is open; leave the answer alone.
      if (el.classList.contains("is-expanded")) return;
      el.classList.remove("is-clamped");
      fitSnippet(el);
      if (el.scrollHeight > el.clientHeight + 1) el.classList.add("is-clamped");
    });
  }

  // Open one card's summary in place. Delegated for the same reason as the glance
  // retry above: the feed's infinite scroll swaps in cards that did not exist when
  // any listener was bound.
  document.addEventListener("click", function (e) {
    var button = e.target.closest ? e.target.closest("[data-card-expand]") : null;
    if (!button) return;
    var snippet = document.getElementById(button.getAttribute("aria-controls"));
    if (!snippet) return;
    var open = !snippet.classList.contains("is-expanded");
    snippet.classList.toggle("is-expanded", open);
    button.setAttribute("aria-expanded", open ? "true" : "false");
    var label = button.querySelector("[data-card-expand-label]");
    if (label) label.textContent = open ? "Show less" : "Show more";
    // Collapsing can leave the card's own top above the viewport - the reader
    // pressed a button that was on screen and would otherwise land mid-feed with
    // no idea which card moved. Only when it actually scrolled off.
    if (!open) {
      var card = button.closest(".card");
      if (card && card.getBoundingClientRect().top < 0) {
        card.scrollIntoView({ block: "start", behavior: "smooth" });
      }
    }
  });

  // The measurement above is wrong until the webfont it measured against is the
  // one actually painted. `document.fonts.ready` resolves once, after that, and
  // re-running for the whole document then is cheaper than guessing a delay.
  if (document.fonts && document.fonts.ready) {
    document.fonts.ready.then(function () { markClampedText(document); });
  }
  window.addEventListener("resize", function () { markClampedText(document); });

  /* ===================== per-load initialization ======================== */

  function onLoad(root) {
    root = root || document;
    pickAll(root, ".post-audio[data-post-id]").forEach(initNarration);
    pickAll(root, "form[data-weight-form]").forEach(initWeights);
    initQueueGroups(root);
    initTopicSearch(root);
    retimeAgo(root);
    hydrateRich(root);
    initChat(root);
    initBenchForm(root);
    markClampedText(root);
    // Queried from the document, not `root`: the pane may have been removed by a
    // swap somewhere else entirely, and that stream still needs closing.
    syncLogStream();
    syncBenchProgress();
    if (document.querySelector(".admin-sidebar-nav")) syncNav();
  }

  // htmx fires htmx:load for the initial document and every swapped fragment.
  document.body.addEventListener("htmx:load", function (e) {
    onLoad(e.detail && e.detail.elt ? e.detail.elt : document);
  });
})();
