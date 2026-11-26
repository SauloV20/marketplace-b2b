// Janela do atendente virtual: a conversa fica só nesta aba (sessionStorage) e é enviada ao servidor a cada pergunta.
(function () {
  const raiz = document.getElementById('chat');
  if (!raiz) return;
  const janela = document.getElementById('chat-janela');
  const abrir = raiz.querySelector('.chat-abrir');
  const lista = document.getElementById('chat-msgs');
  const form = document.getElementById('chat-form');
  const texto = document.getElementById('chat-texto');
  const sugestoes = document.getElementById('chat-sugestoes');
  const CHAVE = 'marketplace-chat';
  let historico = [];
  try { historico = JSON.parse(sessionStorage.getItem(CHAVE) || '[]'); } catch (e) { historico = []; }
  const guardar = () => { try { sessionStorage.setItem(CHAVE, JSON.stringify(historico.slice(-20))); } catch (e) {} };

  // texto do modelo -> HTML seguro: escapa tudo, depois cria links da loja/https e negrito simples
  // (link "/..." só da própria loja: "//site" e "/\site" levariam para outro site)
  function formatar(t) {
    const esc = t.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
    return esc
      .replace(/\*\*([^*\n]+)\*\*/g, '<b>$1</b>')
      .replace(/\[([^\]\n]+)\]\((\/(?![\/\\])[^\s)]*|https:\/\/[^\s)]+)\)/g, '<a href="$2">$1</a>')
      .replace(/(^|[\s(])(\/(?:produto|conta|cadastro|entrar|privacidade|termos|carrinho)[^\s)<,]*|\/\?cat=\d+)/g, '$1<a href="$2">$2</a>')
      .replace(/(^|[\s(])(https:\/\/[^\s<)]+)/g, '$1<a href="$2" target="_blank" rel="noopener">$2</a>')
      .replace(/\n/g, '<br>');
  }
  function bolha(papel, conteudo, extra) {
    const div = document.createElement('div');
    div.className = 'chat-msg ' + (papel === 'user' ? 'eu' : 'bot') + (extra ? ' ' + extra : '');
    if (papel === 'user') div.textContent = conteudo; else div.innerHTML = formatar(conteudo);
    lista.append(div);
    lista.scrollTop = lista.scrollHeight;
    return div;
  }
  function boasVindas() {
    const nome = raiz.dataset.nome;
    bolha('assistant', (nome ? 'Olá, ' + nome + '! ' : 'Olá! ') + 'Sou o atendente virtual do Marketplace B2B. Posso ajudar a achar produtos, explicar preços de atacado, cadastro, pagamento e pedidos.');
  }
  function mostrarTudo() {
    lista.innerHTML = '';
    boasVindas();
    historico.forEach(m => bolha(m.role, m.content));
    sugestoes.hidden = historico.length > 0;
  }
  function alternar(aberto) {
    janela.hidden = !aberto;
    abrir.setAttribute('aria-expanded', String(aberto));
    raiz.classList.toggle('aberto', aberto);
    if (aberto) { mostrarTudo(); texto.focus(); }
  }
  abrir.addEventListener('click', () => alternar(janela.hidden));
  raiz.querySelector('.chat-fechar').addEventListener('click', () => { alternar(false); abrir.focus(); });
  document.addEventListener('keydown', e => { if (e.key === 'Escape' && !janela.hidden) alternar(false); });

  let ocupado = false;
  async function enviar(pergunta) {
    pergunta = pergunta.trim();
    if (!pergunta || ocupado) return;
    ocupado = true;
    sugestoes.hidden = true;
    historico.push({ role: 'user', content: pergunta });
    guardar();
    bolha('user', pergunta);
    texto.value = '';
    const pensando = bolha('assistant', 'Digitando...', 'pensando');
    const fd = new FormData();
    fd.append('historico', JSON.stringify(historico.slice(-12)));
    fd.append('_csrf', document.querySelector('meta[name=csrf]').content);
    try {
      const r = await fetch(raiz.dataset.url, { method: 'POST', body: fd });
      const d = await r.json();
      pensando.remove();
      if (d.ok) {
        historico.push({ role: 'assistant', content: d.resposta });
        guardar();
        bolha('assistant', d.resposta);
      } else {
        historico.pop(); guardar();
        bolha('assistant', d.msg || 'Não consegui responder agora.', 'erro');
      }
    } catch (e) {
      pensando.remove();
      historico.pop(); guardar();
      bolha('assistant', 'Não consegui me conectar. Tente de novo em instantes.', 'erro');
    }
    ocupado = false;
    texto.focus();
  }
  form.addEventListener('submit', e => { e.preventDefault(); enviar(texto.value); });
  texto.addEventListener('keydown', e => { if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); enviar(texto.value); } });
  sugestoes.querySelectorAll('button').forEach(b => b.addEventListener('click', () => enviar(b.textContent)));
})();
