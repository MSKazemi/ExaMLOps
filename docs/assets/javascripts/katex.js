// Renders the math on the Algorithms pages (pymdownx.arithmatex, generic mode + KaTeX).
//
// Only elements arithmatex marked are rendered — never the whole page body. Auto-rendering the
// body would turn every "$5 and $10" or unquoted "$HOME" in the other guides into broken math.
// Inline math is written \( … \) and display math $$ … $$ (see mkdocs.yml, inline_syntax).
// `document$` is Material's page observable, so this also re-renders after instant navigation.
document$.subscribe(() => {
  if (typeof renderMathInElement !== "function") return; // KaTeX blocked or offline: show TeX
  document.querySelectorAll(".arithmatex").forEach((el) => {
    renderMathInElement(el, {
      delimiters: [
        { left: "\\(", right: "\\)", display: false },
        { left: "\\[", right: "\\]", display: true },
      ],
      throwOnError: false,
    });
  });
});
