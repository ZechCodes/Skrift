/**
 * SkriftNotifications — real-time notification client using Server-Sent Events.
 *
 * Connects to /notifications/stream on page load and window focus,
 * disconnects on blur. Handles deduplication across reconnects and
 * provides built-in "generic" toast UI.
 */
(function () {
    "use strict";

    class SkriftNotifications {
        constructor() {
            this._es = null;
            this._displayedIds = new Set();
            this._pendingSyncIds = new Set();
            this._synced = false;
            this._queue = [];
            this._visibleCount = 0;
            this._container = null;

            this._onFocus = () => this._connect();
            this._onBlur = () => this._disconnect();

            window.addEventListener("focus", this._onFocus);
            window.addEventListener("blur", this._onBlur);
            this._connect();
        }

        get _maxVisible() {
            return window.innerWidth < 768 ? 2 : 3;
        }

        _connect() {
            if (this._es) return;
            this._synced = false;
            this._pendingSyncIds = new Set();

            this._es = new EventSource("/notifications/stream");

            this._es.addEventListener("notification", (e) => {
                let data;
                try {
                    data = JSON.parse(e.data);
                } catch {
                    return;
                }
                this._handleNotification(data);
            });

            this._es.addEventListener("sync", () => {
                this._onSync();
            });

            this._es.onerror = () => {
                this._disconnect();
                // Reconnect after a delay
                setTimeout(() => this._connect(), 5000);
            };
        }

        _disconnect() {
            if (this._es) {
                this._es.close();
                this._es = null;
            }
        }

        _handleNotification(data) {
            if (data.type === "dismissed") {
                this._removeDismissed(data.id);
                return;
            }

            // Pre-sync: track IDs for deduplication
            if (!this._synced) {
                this._pendingSyncIds.add(data.id);
            }

            // Skip duplicates
            if (this._displayedIds.has(data.id)) return;

            // Dispatch cancelable custom event
            const event = new CustomEvent("sk:notification", {
                detail: data,
                cancelable: true,
                bubbles: true,
            });
            const allowed = document.dispatchEvent(event);

            if (allowed && data.type === "generic") {
                this._enqueueGeneric(data);
            }

            this._displayedIds.add(data.id);
        }

        _onSync() {
            this._synced = true;
            // Any locally displayed ID NOT in _pendingSyncIds was dismissed elsewhere
            const toRemove = [];
            for (const id of this._displayedIds) {
                if (!this._pendingSyncIds.has(id)) {
                    toRemove.push(id);
                }
            }
            for (const id of toRemove) {
                this._removeDismissed(id);
            }
            this._pendingSyncIds = new Set();
        }

        _ensureContainer() {
            if (this._container) return this._container;
            this._container = document.getElementById("sk-notifications");
            if (!this._container) {
                this._container = document.createElement("div");
                this._container.id = "sk-notifications";
                this._container.className = "sk-notifications";
                document.body.appendChild(this._container);
            }
            return this._container;
        }

        _enqueueGeneric(data) {
            if (this._visibleCount < this._maxVisible) {
                this._showGeneric(data);
            } else {
                this._queue.push(data);
            }
        }

        _showGeneric(data) {
            const container = this._ensureContainer();
            this._visibleCount++;

            const article = document.createElement("article");
            article.className = "sk-notification";
            article.dataset.notificationId = data.id;

            const content = document.createElement("div");
            content.className = "sk-notification-content";

            if (data.title) {
                const title = document.createElement("div");
                title.className = "sk-notification-title";
                title.textContent = data.title;
                content.appendChild(title);
            }

            if (data.message) {
                const message = document.createElement("div");
                message.className = "sk-notification-message";
                message.textContent = data.message;
                content.appendChild(message);
            }

            article.appendChild(content);

            const dismiss = document.createElement("button");
            dismiss.type = "button";
            dismiss.className = "sk-notification-dismiss";
            dismiss.setAttribute("aria-label", "Dismiss");
            dismiss.innerHTML = "&times;";
            dismiss.addEventListener("click", () => this._dismiss(data.id));
            article.appendChild(dismiss);

            container.appendChild(article);
        }

        _dismiss(id) {
            const el = this._container?.querySelector(
                `[data-notification-id="${id}"]`
            );
            if (!el) return;

            el.classList.add("sk-notification-exit");
            el.addEventListener("animationend", () => {
                el.remove();
                this._visibleCount--;
                this._displayedIds.delete(id);
                this._showNextFromQueue();
            }, { once: true });

            fetch(`/notifications/${id}`, { method: "DELETE" });
        }

        _removeDismissed(id) {
            const el = this._container?.querySelector(
                `[data-notification-id="${id}"]`
            );
            if (!el) {
                // May be in overflow queue
                this._queue = this._queue.filter((d) => d.id !== id);
                this._displayedIds.delete(id);
                return;
            }

            el.classList.add("sk-notification-exit");
            el.addEventListener("animationend", () => {
                el.remove();
                this._visibleCount--;
                this._displayedIds.delete(id);
                this._showNextFromQueue();
            }, { once: true });
        }

        _showNextFromQueue() {
            if (this._queue.length > 0 && this._visibleCount < this._maxVisible) {
                const next = this._queue.shift();
                this._showGeneric(next);
            }
        }
    }

    // Auto-initialize
    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", () => {
            window.__skriftNotifications = new SkriftNotifications();
        });
    } else {
        window.__skriftNotifications = new SkriftNotifications();
    }
})();
