// The only script on the site, and it exists so the templates can carry no inline
// handlers -- which is what lets the Content-Security-Policy forbid inline script
// outright instead of weakening itself with 'unsafe-inline'.
document.addEventListener("change", (e) => {
  const to = e.target.dataset.navigate;        // <select data-navigate="/w/x/branch/">
  if (to) location.assign(to + encodeURIComponent(e.target.value));
});

document.addEventListener("submit", (e) => {
  const ask = e.target.dataset.confirm;        // <form data-confirm="Drop table x?">
  if (ask && !confirm(ask)) e.preventDefault();
});

// Progressive enhancement: the size box works without this -- it is simply always
// visible and validated on the server. With JS, it disappears for types that have no
// size, so the form never offers a field that can only be wrong.
function syncSize(select) {
  const takesSize = (select.dataset.sizes || "").split(",").includes(select.value);
  const box = select.closest(".grid")?.querySelector(".typesize");
  if (box) box.hidden = !takesSize;
}
document.addEventListener("change", (e) => {
  if (e.target.classList.contains("typebase")) syncSize(e.target);
});
document.addEventListener("DOMContentLoaded", () =>
  document.querySelectorAll(".typebase").forEach(syncSize));
