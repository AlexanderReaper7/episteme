/* Hydration for interactive post sections (quiz, chart, diagram).
   Loaded only on post pages that contain at least one of these types; the heavy
   renderers (vega*, mermaid) are separate vendored scripts included only when
   their section type is present. Everything degrades: a failed chart/diagram
   collapses to its caption instead of showing an error blob. */
(function () {
  "use strict";

  /* --- quiz: one-shot multiple choice with reveal ------------------------- */
  document.querySelectorAll(".section-quiz").forEach(function (quiz) {
    var answer = parseInt(quiz.dataset.answer, 10);
    var choices = quiz.querySelectorAll(".quiz-choice");
    choices.forEach(function (btn) {
      btn.addEventListener("click", function () {
        if (quiz.classList.contains("quiz-answered")) return;
        quiz.classList.add("quiz-answered");
        var picked = parseInt(btn.dataset.index, 10);
        btn.classList.add(picked === answer ? "quiz-correct" : "quiz-wrong");
        choices.forEach(function (other) {
          other.disabled = true;
          if (parseInt(other.dataset.index, 10) === answer) {
            other.classList.add("quiz-correct");
          }
        });
        var explanation = quiz.querySelector(".quiz-explanation");
        if (explanation) explanation.hidden = false;
      });
    });
  });

  /* --- chart: vega-lite via vega-embed, dark theme ------------------------ */
  if (window.vegaEmbed) {
    document.querySelectorAll(".section-chart[data-spec]").forEach(function (figure) {
      var spec;
      try {
        spec = JSON.parse(figure.dataset.spec);
      } catch (err) {
        figure.classList.add("render-failed");
        return;
      }
      window
        .vegaEmbed(figure.querySelector(".chart-target"), spec, {
          actions: false,
          theme: "dark",
          config: { background: "transparent" },
        })
        .catch(function () {
          figure.classList.add("render-failed");
        });
    });
  }

  /* --- diagram: mermaid, dark theme --------------------------------------- */
  if (window.mermaid) {
    window.mermaid.initialize({
      startOnLoad: false,
      theme: "dark",
      darkMode: true,
      securityLevel: "strict",
    });
    window.mermaid
      .run({ querySelector: ".section-diagram pre.mermaid" })
      .catch(function () {
        document
          .querySelectorAll(".section-diagram pre.mermaid")
          .forEach(function (pre) {
            if (!pre.querySelector("svg")) {
              pre.closest(".section-diagram").classList.add("render-failed");
            }
          });
      });
  }
})();
