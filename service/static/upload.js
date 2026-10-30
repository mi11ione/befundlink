// Progressive enhancement for the upload page: show the chosen file name, highlight on drag.
// The page works without it (the native input and the CSS :valid state carry the form).
(function () {
  var input = document.getElementById('file'), name = document.getElementById('file-name'), drop = document.getElementById('drop');
  if (!input || !name || !drop) return;
  function show() {
    var f = input.files && input.files[0];
    name.textContent = f ? f.name : '';
  }
  input.addEventListener('change', show);
  ['dragenter', 'dragover'].forEach(function (e) { input.addEventListener(e, function () { drop.classList.add('over'); }); });
  ['dragleave', 'drop'].forEach(function (e) { input.addEventListener(e, function () { drop.classList.remove('over'); }); });
  show();
})();
