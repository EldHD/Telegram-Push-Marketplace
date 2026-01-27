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
    const localeList = document.getElementById('locale-status-list');
    if (localeList && Array.isArray(data.locales)) {
      localeList.innerHTML = data.locales
        .map((row) => {
          return `<div class="locale-pill">${row.locale}: ok ${row.ok}/${row.total}</div>`;
        })
        .join('');
    }
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
  const validatedField = document.getElementById('token-validated');
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
    if (validatedField) validatedField.value = 'false';
    setStatus('', null);
  };

  const tokenRegex = /^\d{6,12}:[A-Za-z0-9_-]{20,}$/;
  const usernameRegex = /^@?[a-z0-9_]{5,64}bot$/i;

  const validateFormat = () => {
    const botUsername = usernameInput.value.trim();
    const token = tokenInput.value.trim();
    if (!botUsername || !token) {
      resetValidation();
      return false;
    }
    if (!usernameRegex.test(botUsername)) {
      resetValidation();
      setStatus('❌ Username must end with "bot".', 'error');
      return false;
    }
    if (!tokenRegex.test(token)) {
      resetValidation();
      setStatus('❌ Invalid token format.', 'error');
      return false;
    }
    return true;
  };

  const validateToken = async () => {
    const botUsername = usernameInput.value.trim();
    const token = tokenInput.value.trim();
    if (!validateFormat()) return;
    isPending = true;
    saveButton.disabled = true;
    setStatus('Validating token...', null);
    try {
      const response = await fetch('/api/bots/validate-token', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ username: botUsername, token }),
      });
      const data = await response.json();
      if (!response.ok || !data.ok) {
        if (data.reason === 'username_mismatch' && data.actual_username) {
          const entered = botUsername.replace('@', '');
          throw new Error(`Token belongs to @${data.actual_username}, not @${entered}`);
        }
        if (data.reason === 'invalid_format') {
          throw new Error('Invalid token format');
        }
        throw new Error('Invalid token');
      }
      isValid = true;
      const namePart = data.bot_name ? `, name=${data.bot_name}` : '';
      setStatus(`✅ This token belongs to ${data.bot_username} (id ${data.bot_id}${namePart})`, 'success');
      if (validatedField) validatedField.value = 'true';
      saveButton.disabled = false;
    } catch (error) {
      isValid = false;
      setStatus(`❌ ${error.message}`, 'error');
      saveButton.disabled = true;
    } finally {
      isPending = false;
    }
  };

  usernameInput.addEventListener('input', () => {
    resetValidation();
    validateFormat();
  });
  tokenInput.addEventListener('input', () => {
    resetValidation();
    validateFormat();
  });
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

function initWizardSteps() {
  const wizard = document.querySelector('.wizard');
  if (!wizard) return;
  const currentStep = Number(wizard.getAttribute('data-current-step') || 1);
  document.querySelectorAll('.wizard-step').forEach((step) => {
    const stepNumber = Number(step.getAttribute('data-step'));
    step.style.display = stepNumber === currentStep ? 'block' : 'none';
  });
}

window.addEventListener('load', initWizardSteps);
