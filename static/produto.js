// Galeria da página do produto: clicar na miniatura troca a foto grande.
(function () {
  const grande = document.getElementById('foto-grande');
  if (!grande) return;
  document.querySelectorAll('[data-foto]').forEach(b => b.addEventListener('click', () => {
    grande.src = b.dataset.foto;
    document.querySelectorAll('[data-foto]').forEach(x => x.toggleAttribute('aria-current', x === b));
  }));
})();
