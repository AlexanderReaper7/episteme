/* Post narration: streaming playback + continuous ("podcast") mode.

   The <audio> src points at /api/posts/{id}/audio/stream?voice=… — the server
   serves a cached file when one exists (seekable) or synthesizes live (opus over
   Fish's WebSocket), streaming as it goes and caching the result. preload="none"
   means nothing is fetched or synthesized until the reader actually presses play.

   Continuous mode: when a post's narration ends, fetch the next feature post and
   navigate to it with ?autoplay=1 so it starts playing straight away — the feed
   plays through like a podcast. The toggle and last-chosen voice persist in
   localStorage so the preference carries across posts. */
(function () {
  "use strict";

  var root = document.querySelector(".post-audio[data-post-id]");
  if (!root) return;

  var postId = root.dataset.postId;
  var select = root.querySelector(".voice-select");
  var player = root.querySelector(".post-audio-player");
  var statusEl = root.querySelector(".voice-status");
  var continuous = root.querySelector(".continuous-toggle");

  var CONT_KEY = "episteme:narrate:continuous";
  var VOICE_KEY = "episteme:narrate:voice";
  var params = new URLSearchParams(window.location.search);

  function setStatus(msg) {
    statusEl.textContent = msg || "";
  }
  function voice() {
    return select ? select.value : "";
  }
  function selectVoice(v) {
    if (!v) return;
    for (var i = 0; i < select.options.length; i++) {
      if (select.options[i].value === v) {
        select.value = v;
        return;
      }
    }
  }
  function loadSource() {
    // preload="none" keeps this from fetching until play() / the controls fire.
    player.src =
      "/api/posts/" + postId + "/audio/stream?voice=" + encodeURIComponent(voice());
  }

  // Restore persisted preferences (query params below can still override).
  try {
    if (localStorage.getItem(CONT_KEY) === "1") continuous.checked = true;
    selectVoice(localStorage.getItem(VOICE_KEY));
  } catch (e) {}

  continuous.addEventListener("change", function () {
    try {
      localStorage.setItem(CONT_KEY, continuous.checked ? "1" : "0");
    } catch (e) {}
  });

  select.addEventListener("change", function () {
    try {
      localStorage.setItem(VOICE_KEY, voice());
    } catch (e) {}
    var wasPlaying = !player.paused && !player.ended;
    loadSource();
    setStatus("");
    if (wasPlaying) player.play().catch(function () {});
  });

  player.addEventListener("playing", function () {
    setStatus("");
  });
  player.addEventListener("waiting", function () {
    setStatus("Buffering…");
  });
  player.addEventListener("error", function () {
    setStatus("Narration failed — try again.");
  });

  player.addEventListener("ended", function () {
    if (!continuous.checked) return;
    setStatus("Loading next…");
    fetch("/api/posts/" + postId + "/next")
      .then(function (r) {
        return r.json();
      })
      .then(function (d) {
        if (d && d.next_id) {
          window.location.href =
            "/post/" + d.next_id +
            "?autoplay=1&continuous=1&voice=" + encodeURIComponent(voice());
        } else {
          setStatus("End of feed.");
        }
      })
      .catch(function () {
        setStatus("");
      });
  });

  // Auto-advance landing: ?autoplay=1 (&voice=&continuous=1) → start immediately.
  if (params.get("continuous") === "1") {
    continuous.checked = true;
    try {
      localStorage.setItem(CONT_KEY, "1");
    } catch (e) {}
  }
  selectVoice(params.get("voice"));
  loadSource();

  if (params.get("autoplay") === "1") {
    // Autoplay after a navigation may be blocked without a fresh gesture; if so,
    // the reader just presses play. (Frequent listeners get autoplay allowed.)
    player.play().catch(function () {
      setStatus("Press play to continue.");
    });
  }
})();
