/**
 * Blog real-time post insertion.
 *
 * Listens for sk:notification events with type "new_post" and
 * inserts new post cards into the grid without a page reload.
 */
(function () {
  const seenSlugs = new Set();
  const grid = document.getElementById("post-grid");
  const tpl = document.getElementById("post-card-template");

  // Seed deduplication set from existing cards
  if (grid) {
    grid.querySelectorAll("[data-post-slug]").forEach(function (el) {
      seenSlugs.add(el.getAttribute("data-post-slug"));
    });
  }

  document.addEventListener("sk:notification", function (e) {
    var data = e.detail;
    if (data.type !== "new_post") return;
    if (!grid || !tpl) return;

    // Deduplicate
    if (seenSlugs.has(data.slug)) return;
    seenSlugs.add(data.slug);

    // Suppress default toast
    e.preventDefault();

    // Clone template and fill in data
    var clone = tpl.content.cloneNode(true);

    var titleLink = clone.querySelector(".blog-post-title a");
    if (titleLink) {
      titleLink.textContent = data.title;
      titleLink.href = "/post/" + data.slug;
    }

    var dateEl = clone.querySelector(".blog-post-date");
    if (dateEl && data.published_at) {
      var d = new Date(data.published_at);
      dateEl.textContent = d.toLocaleDateString("en-US", {
        year: "numeric",
        month: "long",
        day: "numeric",
      });
    }

    var excerpt = clone.querySelector(".blog-post-excerpt");
    if (excerpt) {
      excerpt.textContent = data.meta_description || "";
    }

    var card = clone.querySelector(".blog-post-card");
    if (card) {
      card.setAttribute("data-post-slug", data.slug);
      card.style.opacity = "0";
      card.style.transform = "translateY(-10px)";
    }

    // Prepend to grid
    grid.insertBefore(clone, grid.firstChild);

    // Fade-in animation
    var inserted = grid.querySelector('[data-post-slug="' + data.slug + '"]');
    if (inserted) {
      requestAnimationFrame(function () {
        inserted.style.transition = "opacity 0.4s ease, transform 0.4s ease";
        inserted.style.opacity = "1";
        inserted.style.transform = "translateY(0)";
      });
    }
  });
})();
