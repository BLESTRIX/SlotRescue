window.addEventListener('DOMContentLoaded', () => {
  SwaggerUIBundle({
    url: '/openapi.json', dom_id: '#swagger-ui', deepLinking: true, validatorUrl: null,
    presets: [SwaggerUIBundle.presets.apis],
    requestInterceptor: request => {
      const token = document.cookie.split('; ').find(item => item.startsWith('csrf='));
      if (token) request.headers['X-CSRF-Token'] = decodeURIComponent(token.substring(5));
      return request;
    }
  });
});
