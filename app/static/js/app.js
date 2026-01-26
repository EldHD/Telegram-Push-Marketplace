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

function initBotTokenValidation() {
  const usernameInput = document.getElementById('bot-username');
  const tokenInput = document.getElementById('bot-token');
  const saveButton = document.getElementById('save-bot-button');
  const validateButton = document.getElementById('validate-bot-token');
  const status = document.getElementById('token-validation-status');
  const form = document.querySelector('form[action=\"/bot-owner/bots\"]');
  if (!usernameInput || !tokenInput || !saveButton || !validateButton || !status || !form) {
    return;
  }

  let isValid = false;
  let isPending = false;

  const setStatus = (message, type) => {
    status.textContent = message;
    status.classList.remove('success', 'error');
    if (type) status.classList.add(type);
  };

  const resetValidation = () => {
    isValid = false;
    isPending = false;
    saveButton.disabled = true;
    setStatus('', null);
  };

  const validateToken = async () => {
    const botUsername = usernameInput.value.trim();
    const token = tokenInput.value.trim();
    if (!botUsername || !token) {
      resetValidation();
      return;
    }
    isPending = true;
    saveButton.disabled = true;
    setStatus('Validating token...', null);
    try {
      const response = await fetch('/bot-owner/validate-token', {
      const response = await fetch('/api/bots/validate', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ bot_username: botUsername, token }),
      });
      const data = await response.json();
      if (!response.ok || !data.ok) {
        throw new Error(data.error || data.detail || 'Validation failed');
      }
      isValid = true;
      const namePart = data.bot.name ? `, name=${data.bot.name}` : '';
      setStatus(`✅ Это реально бот ${data.bot.username} (id=${data.bot.id}${namePart})`, 'success');
      saveButton.disabled = false;
    } catch (error) {
      isValid = false;
      setStatus(`❌ ${error.message}`, 'error');
      if (!response.ok) {
        throw new Error(data.detail || 'Validation failed');
      }
      isValid = true;
      setStatus(`Confirmed: this token belongs to ${data.username}`, 'success');
      saveButton.disabled = false;
    } catch (error) {
      isValid = false;
      setStatus(error.message, 'error');
      saveButton.disabled = true;
    } finally {
      isPending = false;
    }
  };

  usernameInput.addEventListener('input', resetValidation);
  tokenInput.addEventListener('input', resetValidation);
  tokenInput.addEventListener('blur', validateToken);
  validateButton.addEventListener('click', validateToken);

  form.addEventListener('submit', (event) => {
    if (!isValid || isPending) {
      event.preventDefault();
      if (!isPending) {
        setStatus('Please validate the token before saving.', 'error');
      }
    }
  });
}

window.addEventListener('load', initBotTokenValidation);
