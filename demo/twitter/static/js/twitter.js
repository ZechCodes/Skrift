/* The Feed — interactions & micro-animations */

/* ── Client-side HTML sanitizer (defense-in-depth) ── */
function sanitizeHTML(html) {
    const doc = new DOMParser().parseFromString(html, 'text/html');
    const allowed = { A: ['href', 'rel', 'target'] };

    function clean(parent) {
        for (const node of Array.from(parent.childNodes)) {
            if (node.nodeType === Node.TEXT_NODE) continue;
            if (node.nodeType !== Node.ELEMENT_NODE) {
                node.remove();
                continue;
            }
            const tag = node.tagName;
            if (!(tag in allowed)) {
                // Replace disallowed element with its text content
                node.replaceWith(document.createTextNode(node.textContent));
                continue;
            }
            // Strip disallowed attributes
            for (const attr of Array.from(node.attributes)) {
                if (!allowed[tag].includes(attr.name)) {
                    node.removeAttribute(attr.name);
                }
            }
            // Block javascript: URIs
            if (node.hasAttribute('href') && /^\s*javascript\s*:/i.test(node.getAttribute('href'))) {
                node.removeAttribute('href');
            }
            clean(node);
        }
    }

    clean(doc.body);
    return doc.body.innerHTML;
}

/* ── SVG icon constants (match _tweet_card.html) ── */
const ICON_REPLY = '<svg viewBox="0 0 24 24" width="16" height="16"><path fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" d="M21 11.5a8.38 8.38 0 01-.9 3.8 8.5 8.5 0 01-7.6 4.7 8.38 8.38 0 01-3.8-.9L3 21l1.9-5.7a8.38 8.38 0 01-.9-3.8 8.5 8.5 0 014.7-7.6 8.38 8.38 0 013.8-.9h.5a8.48 8.48 0 018 8v.5z"/></svg>';
const ICON_RETWEET = '<svg viewBox="0 0 24 24" width="16" height="16"><path fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" d="M17 1l4 4-4 4M3 11V9a4 4 0 014-4h14M7 23l-4-4 4-4m14 0v2a4 4 0 01-4 4H3"/></svg>';
const ICON_LIKE = '<svg viewBox="0 0 24 24" width="16" height="16"><path fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" d="M20.84 4.61a5.5 5.5 0 00-7.78 0L12 5.67l-1.06-1.06a5.5 5.5 0 00-7.78 7.78l1.06 1.06L12 21.23l7.78-7.78 1.06-1.06a5.5 5.5 0 000-7.78z"/></svg>';
const ICON_BOOKMARK = '<svg viewBox="0 0 24 24" width="16" height="16"><path fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" d="M19 21l-7-5-7 5V5a2 2 0 012-2h10a2 2 0 012 2z"/></svg>';
const ICON_SHARE = '<svg viewBox="0 0 24 24" width="16" height="16"><path fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" d="M4 12v8a2 2 0 002 2h12a2 2 0 002-2v-8M16 6l-4-4-4 4M12 2v13"/></svg>';

/* ── Get CSRF token from an existing form on the page ── */
function getCsrfToken() {
    const input = document.querySelector('input[name="_csrf"]');
    return input ? input.value : null;
}

/* ── Create an action form that POSTs to a URL ── */
function buildActionForm(action, csrfToken, cssClass, title, icon, countText) {
    const form = document.createElement('form');
    form.method = 'post';
    form.action = action;
    form.className = 'tweet-action-form';

    const hidden = document.createElement('input');
    hidden.type = 'hidden';
    hidden.name = '_csrf';
    hidden.value = csrfToken;
    form.appendChild(hidden);

    const btn = document.createElement('button');
    btn.type = 'submit';
    btn.className = 'tweet-action ' + cssClass;
    btn.title = title;
    btn.innerHTML = icon + (countText != null ? '<span>' + countText + '</span>' : '');
    form.appendChild(btn);

    return form;
}

/* ── Build a tweet card DOM element ── */
function buildTweetCard(data) {
    const csrfToken = getCsrfToken();
    const tweetUrl = '/tweet/' + data.tweet_id;

    const article = document.createElement('article');
    article.className = 'tweet-card tweet-card-new';
    article.dataset.tweetId = data.tweet_id;

    const inner = document.createElement('div');
    inner.className = 'tweet-card-inner';

    // Avatar
    const avatarDiv = document.createElement('div');
    avatarDiv.className = 'tweet-avatar';
    const avatarLink = document.createElement('a');
    avatarLink.href = '/profile/' + data.user_id;
    if (data.user_picture_url) {
        const img = document.createElement('img');
        img.src = data.user_picture_url;
        img.alt = data.user_name;
        img.referrerPolicy = 'no-referrer';
        avatarLink.appendChild(img);
    } else {
        const placeholder = document.createElement('div');
        placeholder.className = 'avatar-placeholder';
        placeholder.textContent = (data.user_name || 'U')[0];
        avatarLink.appendChild(placeholder);
    }
    avatarDiv.appendChild(avatarLink);

    // Body
    const body = document.createElement('div');
    body.className = 'tweet-body';

    // Header
    const header = document.createElement('div');
    header.className = 'tweet-header';
    const authorLink = document.createElement('a');
    authorLink.href = '/profile/' + data.user_id;
    authorLink.className = 'tweet-author';
    authorLink.textContent = data.user_name;
    const timeSpan = document.createElement('span');
    timeSpan.className = 'tweet-time';
    timeSpan.textContent = data.created_at;
    header.appendChild(authorLink);
    header.appendChild(timeSpan);

    // Content (server-rendered, escaped HTML)
    const content = document.createElement('div');
    content.className = 'tweet-content';
    content.innerHTML = sanitizeHTML(data.content_html);

    // Actions
    const actions = document.createElement('div');
    actions.className = 'tweet-actions';

    // Reply — always a link
    const replyLink = document.createElement('a');
    replyLink.href = tweetUrl;
    replyLink.className = 'tweet-action tweet-action-reply';
    replyLink.title = 'Reply';
    replyLink.innerHTML = ICON_REPLY + '<span>0</span>';
    actions.appendChild(replyLink);

    if (csrfToken) {
        // Logged in — build action forms
        actions.appendChild(buildActionForm(
            tweetUrl + '/retweet', csrfToken,
            'tweet-action-retweet', 'Retweet', ICON_RETWEET, '0'
        ));
        actions.appendChild(buildActionForm(
            tweetUrl + '/like', csrfToken,
            'tweet-action-like', 'Like', ICON_LIKE, '0'
        ));
        actions.appendChild(buildActionForm(
            tweetUrl + '/bookmark', csrfToken,
            'tweet-action-bookmark', 'Save', ICON_BOOKMARK, null
        ));
    } else {
        // Logged out — static counts
        const rtSpan = document.createElement('span');
        rtSpan.className = 'tweet-action';
        rtSpan.innerHTML = ICON_RETWEET + '<span>0</span>';
        actions.appendChild(rtSpan);

        const likeSpan = document.createElement('span');
        likeSpan.className = 'tweet-action';
        likeSpan.innerHTML = ICON_LIKE + '<span>0</span>';
        actions.appendChild(likeSpan);

        const bookmarkSpan = document.createElement('span');
        bookmarkSpan.className = 'tweet-action';
        bookmarkSpan.innerHTML = ICON_BOOKMARK;
        actions.appendChild(bookmarkSpan);
    }

    // Share — always available
    const shareBtn = document.createElement('button');
    shareBtn.type = 'button';
    shareBtn.className = 'tweet-action tweet-action-share';
    shareBtn.title = 'Share';
    shareBtn.dataset.shareUrl = tweetUrl;
    shareBtn.innerHTML = ICON_SHARE;
    actions.appendChild(shareBtn);

    body.appendChild(header);
    body.appendChild(content);
    body.appendChild(actions);
    inner.appendChild(avatarDiv);
    inner.appendChild(body);
    article.appendChild(inner);

    return article;
}

/* ── Real-time new_tweet listener ── */
document.addEventListener('sk:notification', (e) => {
    const data = e.detail;
    if (data.type !== 'new_tweet') return;

    // Deduplicate against server-rendered cards
    if (document.querySelector('[data-tweet-id="' + CSS.escape(data.tweet_id) + '"]')) return;

    // Find or create the tweet list container
    let list = document.querySelector('.tweet-list');
    if (!list) {
        const empty = document.querySelector('.tweet-empty');
        if (!empty) return; // Not on a feed page
        list = document.createElement('div');
        list.className = 'tweet-list';
        empty.replaceWith(list);
    }

    const card = buildTweetCard(data);
    list.prepend(card);

    // Trigger slide-in animation
    requestAnimationFrame(() => {
        card.classList.remove('tweet-card-new');
    });
});

document.addEventListener('DOMContentLoaded', () => {
    // Character counter for textareas
    document.querySelectorAll('.compose-form textarea, .reply-box textarea').forEach(textarea => {
        const counter = textarea.closest('form').querySelector('.char-counter');
        if (!counter) return;

        const max = parseInt(counter.dataset.max, 10) || 280;

        function update() {
            const remaining = max - textarea.value.length;
            counter.textContent = remaining;
            counter.classList.remove('warning', 'danger');
            if (remaining <= 0) {
                counter.classList.add('danger');
            } else if (remaining <= 20) {
                counter.classList.add('warning');
            }
        }

        textarea.addEventListener('input', update);
        update();
    });

    // Confirm dialogs for delete actions
    document.querySelectorAll('form[data-confirm]').forEach(form => {
        form.addEventListener('submit', (e) => {
            if (!confirm(form.dataset.confirm)) {
                e.preventDefault();
            }
        });
    });

    // Auto-resize textareas
    document.querySelectorAll('.compose-form textarea').forEach(textarea => {
        function resize() {
            textarea.style.height = 'auto';
            textarea.style.height = textarea.scrollHeight + 'px';
        }
        textarea.addEventListener('input', resize);
    });
});

// Share buttons — event delegation so dynamically added buttons work too
document.addEventListener('click', async (e) => {
    const btn = e.target.closest('.tweet-action-share');
    if (!btn) return;

    const url = location.origin + btn.dataset.shareUrl;
    if (navigator.share) {
        try { await navigator.share({ url }); } catch {}
    } else if (navigator.clipboard) {
        await navigator.clipboard.writeText(url);
        btn.classList.add('copied');
        setTimeout(() => btn.classList.remove('copied'), 1500);
    }
});
