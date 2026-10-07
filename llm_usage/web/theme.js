'use strict';
// Applied before the stylesheet paints; unavailable storage does not block the UI.
(() => {
  const choices = ['forest', 'paper', 'charcoal', 'midnight', 'blue', 'violet', 'rose', 'amber', 'ocean', 'system'];
  const system = window.matchMedia('(prefers-color-scheme: dark)');
  let choice = 'forest';
  try { choice = localStorage.getItem('llm-usage-theme') || choice; } catch {}
  if (!choices.includes(choice)) choice = 'forest';
  function apply() {
    document.documentElement.dataset.theme = choice === 'system' ? (system.matches ? 'charcoal' : 'paper') : choice;
    window.dispatchEvent(new Event('llm-theme-change'));
  }
  window.LLMTheme = {
    get choice() { return choice; },
    set(value) {
      if (!choices.includes(value)) return;
      choice = value;
      try { localStorage.setItem('llm-usage-theme', choice); } catch {}
      apply();
    },
  };
  system.addEventListener('change', () => { if (choice === 'system') apply(); });
  apply();
})();
