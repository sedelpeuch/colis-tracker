(function () {
  const KEY = "colis-theme";
  const root = document.documentElement;
  const saved = localStorage.getItem(KEY);
  if (saved) root.setAttribute("data-theme", saved);

  function current() {
    const attr = root.getAttribute("data-theme");
    if (attr) return attr;
    return window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
  }

  function apply() {
    const btn = document.querySelector("[data-theme-toggle]");
    if (btn) btn.textContent = current() === "dark" ? "☀ Clair" : "🌙 Sombre";
  }

  document.addEventListener("click", (event) => {
    const btn = event.target.closest("[data-theme-toggle]");
    if (!btn) return;
    const next = current() === "dark" ? "light" : "dark";
    root.setAttribute("data-theme", next);
    localStorage.setItem(KEY, next);
    apply();
  });

  document.addEventListener("DOMContentLoaded", apply);
})();
