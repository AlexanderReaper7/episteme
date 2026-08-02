/* Single persistent front-end controller.

   The whole app is htmx-boosted (base.html): navigation swaps only the
   #main-content (or admin #admin-main) region and pushes the URL — the page is
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
  // viewport top pushes the page down by the header's height — a spurious downward
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

  // Search `root` INCLUDING itself — htmx:load hands us the swapped element, which
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
  // (vega*, mermaid) are already loaded — hydrateRich guarantees that. Everything
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
        // so nothing the pipeline writes lands here — this guards hand-edited rows.
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
        // chart to nothing — a worse failure than the one being fixed. A 0 reading
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
      setStatus("Narration failed — try again.");
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

    // ?continuous=1 / ?voice= / ?autoplay=1 — the hard-navigation fallback landing.
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
  //   * `dirty` — the slugs actually moved, and the ONLY thing the server records
  //     from. This is the load-bearing part: only the client knows which sliders
  //     moved, because only it holds each one's rendered starting value. A server
  //     comparing submitted values against the stored profile would record every
  //     slider whose weight had decayed past its own rounding since the page
  //     rendered — pinning weights nobody touched. So the list genuinely needs
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
        // as value="-2.0" reads back "-2" and would look moved on first paint —
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

  /* ===================== per-load initialization ======================== */

  function onLoad(root) {
    root = root || document;
    pickAll(root, ".post-audio[data-post-id]").forEach(initNarration);
    pickAll(root, "form[data-weight-form]").forEach(initWeights);
    hydrateRich(root);
    if (document.querySelector(".admin-sidebar-nav")) syncNav();
  }

  // htmx fires htmx:load for the initial document and every swapped fragment.
  document.body.addEventListener("htmx:load", function (e) {
    onLoad(e.detail && e.detail.elt ? e.detail.elt : document);
  });
})();
