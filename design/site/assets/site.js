// Boeklite marketing site: menu, mega menu, audience switch, pricing toggle.
(function () {
  var header = document.querySelector('.site-header');
  var toggle = document.querySelector('.menu-toggle');
  if (toggle) toggle.addEventListener('click', function () {
    var open = header.classList.toggle('open');
    toggle.setAttribute('aria-expanded', open);
  });

  document.querySelectorAll('[data-mega]').forEach(function (btn) {
    var menu = document.getElementById(btn.getAttribute('aria-controls'));
    btn.addEventListener('click', function (e) {
      e.stopPropagation();
      var open = menu.classList.toggle('open');
      btn.setAttribute('aria-expanded', open);
    });
    document.addEventListener('click', function (e) {
      if (!menu.contains(e.target)) { menu.classList.remove('open'); btn.setAttribute('aria-expanded', 'false'); }
    });
    document.addEventListener('keydown', function (e) {
      if (e.key === 'Escape') { menu.classList.remove('open'); btn.setAttribute('aria-expanded', 'false'); }
    });
  });

  document.querySelectorAll('[role="tablist"]').forEach(function (list) {
    var tabs = list.querySelectorAll('[role="tab"]');
    tabs.forEach(function (tab) {
      tab.addEventListener('click', function () {
        tabs.forEach(function (t) {
          var on = t === tab;
          t.setAttribute('aria-selected', on);
          document.getElementById(t.getAttribute('aria-controls')).hidden = !on;
        });
      });
    });
  });

  var billing = document.querySelector('[data-billing]');
  if (billing) billing.addEventListener('click', function () {
    var yearly = billing.getAttribute('aria-checked') !== 'true';
    billing.setAttribute('aria-checked', yearly);
    document.querySelectorAll('[data-month]').forEach(function (el) {
      el.textContent = yearly ? el.getAttribute('data-year') : el.getAttribute('data-month');
    });
    document.querySelectorAll('[data-note]').forEach(function (el) {
      el.textContent = yearly ? el.getAttribute('data-note') : '';
    });
  });
})();
