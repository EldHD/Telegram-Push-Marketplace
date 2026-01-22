async function pollVerification() {
  const container = document.querySelector('[data-verification]');
  if (!container) return;
  const botId = container.getAttribute('data-verification');
  try {
    const resp = await fetch(`/bot-owner/bots/${botId}/verification/status`);
    if (!resp.ok) return;
    const data = await resp.json();
    if (data.status === 'NONE') return;
    const total = data.total || 0;
    const verified = data.verified || 0;
    const progress = total ? Math.round((verified / total) * 100) : 0;
    const progressEl = document.querySelector(`[data-progress="${botId}"]`);
    if (progressEl) progressEl.style.width = `${progress}%`;
    const map = {
      total: total,
      verified: verified,
      ok: data.ok,
      blocked: data.blocked,
      not_started: data.not_started,
      other_error: data.other_error,
      eta: `${(data.eta_seconds / 60).toFixed(1)} min`,
    };
    Object.entries(map).forEach(([key, value]) => {
      const el = document.querySelector(`[data-stat="${key}"]`);
      if (el) el.textContent = value;
    });
  } catch (err) {
    console.warn(err);
  }
}

setInterval(pollVerification, 5000);
window.addEventListener('load', pollVerification);

function initPricingToggles() {
  document.querySelectorAll('[data-sale-toggle]').forEach((toggle) => {
    const locale = toggle.getAttribute('data-sale-toggle');
    const input = document.querySelector(`[data-cpm-input=\"${locale}\"]`);
    if (!input) return;
    const update = () => {
      input.disabled = !toggle.checked;
    };
    toggle.addEventListener('change', update);
    update();
  });
}

window.addEventListener('load', initPricingToggles);
