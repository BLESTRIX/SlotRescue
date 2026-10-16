document.addEventListener('DOMContentLoaded', () => {
  // Retain form input and show readable server errors for HTMX conflicts/validation.
  if (window.htmx) {
    htmx.config.allowEval = false;
    htmx.config.includeIndicatorStyles = false;
    htmx.config.responseHandling = [
      {code: '204', swap: false}, {code: '[23]..', swap: true},
      {code: '[45]..', swap: true, error: true}
    ];
  }
  // Native GET forms omit blank optional filters instead of submitting invalid dates/IDs.
  document.addEventListener('submit', event => {
    if (event.target.method.toLowerCase() !== 'get') return;
    for (const input of event.target.elements) {
      if (input.name && input.value === '') {
        input.disabled = true;
        setTimeout(() => { input.disabled = false; }, 100);
      }
    }
  });
  document.body.addEventListener('htmx:configRequest', event => {
    if (event.detail.verb.toLowerCase() !== 'get') return;
    for (const name of Object.keys(event.detail.parameters)) {
      if (event.detail.parameters[name] === '') delete event.detail.parameters[name];
    }
  });
  document.body.addEventListener('htmx:sendError', () => {
    const feedback = document.getElementById('feedback');
    if (feedback) {
      feedback.className = 'alert alert-danger';
      feedback.textContent = 'Connection interrupted. Please retry. Your booking request keeps the same retry key.';
    }
  });
  for (const element of document.querySelectorAll('[data-countdown]')) {
    const deadline = Date.parse(element.dataset.countdown);
    const serverStart = Date.parse(element.dataset.serverNow);
    const start = performance.now();
    const update = () => {
      const seconds = Math.max(0, Math.floor((deadline - serverStart - (performance.now() - start)) / 1000));
      element.textContent = seconds ? `${Math.floor(seconds / 60)}:${String(seconds % 60).padStart(2, '0')}` : 'Offer expired';
      if (!seconds) {
        for (const button of document.querySelectorAll('form[action*="/accept/"] button')) button.disabled = true;
        clearInterval(timer);
      }
    };
    const timer = setInterval(update, 1000);
    update();
  }
});
