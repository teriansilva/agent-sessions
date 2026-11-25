const q = new URLSearchParams(location.search);
document.documentElement.dataset.theme = q.get('theme') || 'dark';
if (q.get('mobile')) document.body.classList.add('mobile');
