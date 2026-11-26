// Carrossel de banners: passa sozinho a cada 5 s, pausa com o mouse/foco, setas, pontos e arrastar no celular.
(function () {
  const raiz = document.querySelector('.carrossel');
  if (!raiz) return;
  const trilho = raiz.querySelector('.slides');
  const slides = [...trilho.children];
  const pontos = [...raiz.querySelectorAll('.pontos button')];
  if (slides.length < 2) return;
  const calmo = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  let atual = 0, timer = null;
  function ir(i, suave = true) {
    atual = (i + slides.length) % slides.length;
    trilho.scrollTo({ left: slides[atual].offsetLeft, behavior: suave && !calmo ? 'smooth' : 'auto' });
  }
  function marcar() {
    const i = Math.round(trilho.scrollLeft / trilho.clientWidth);
    atual = Math.max(0, Math.min(slides.length - 1, i));
    pontos.forEach((p, k) => p.toggleAttribute('aria-current', k === atual));
  }
  function tocar() { parar(); if (!calmo) timer = setInterval(() => ir(atual + 1), 5000); }
  function parar() { clearInterval(timer); timer = null; }
  raiz.querySelector('.ant').addEventListener('click', () => { ir(atual - 1); tocar(); });
  raiz.querySelector('.prox').addEventListener('click', () => { ir(atual + 1); tocar(); });
  pontos.forEach((p, k) => p.addEventListener('click', () => { ir(k); tocar(); }));
  trilho.addEventListener('scroll', () => window.requestAnimationFrame(marcar), { passive: true });
  raiz.addEventListener('mouseenter', parar);
  raiz.addEventListener('mouseleave', tocar);
  raiz.addEventListener('focusin', parar);
  raiz.addEventListener('focusout', tocar);
  trilho.addEventListener('touchstart', parar, { passive: true });
  document.addEventListener('visibilitychange', () => (document.hidden ? parar() : tocar()));
  tocar();
})();
