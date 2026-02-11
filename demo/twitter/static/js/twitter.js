/* The Feed — interactions & micro-animations */

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
