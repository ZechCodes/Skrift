/**
 * SkriftNotifications — real-time notification client using Server-Sent Events.
 *
 * Connects to /notifications/stream on page load and window focus,
 * disconnects on blur. Handles deduplication across reconnects and
 * provides built-in "generic" toast UI.
 */
(function () {
    "use strict";

    const _modeDefaults = {
        queued:     { dismiss: "server", autoClear: false },
        timeseries: { dismiss: false,    autoClear: 8000 },
        ephemeral:  { dismiss: false,    autoClear: 5000 },
    };

    class SkriftNotifications {
        constructor() {
            this._es = null;
            this._displayedIds = new Set();
            this._pendingSyncIds = new Set();
            this._groupMap = new Map();
            this._synced = false;
            this._status = "disconnected";
            this._queue = [];
            this._visibleCount = 0;
            this._container = null;
            this._statusIndicator = null;
            this._statusHideTimeout = null;
            this._connectingSince = null;
            this._connectingTimeout = null;
            this._lastTimestamp = null;
            this._config = {};

            this._onFocus = () => this._connect();
            this._onBlur = () => {
                if (!this._config.persistConnection) {
                    this._disconnect();
                    this._setStatus("suspended");
                }
            };

            window.addEventListener("focus", this._onFocus);
            window.addEventListener("blur", this._onBlur);
            this._connect();
        }

        configure(options) {
            Object.assign(this._config, options);
        }

        _getModeConfig(mode) {
            return Object.assign(
                {},
                _modeDefaults[mode] || _modeDefaults.queued,
                this._config["*"],
                this._config[mode],
            );
        }

        get _maxVisible() {
            return window.innerWidth < 768 ? 2 : 3;
        }

        get status() {
            return this._status;
        }

        _setStatus(status) {
            if (this._status === status) return;
            this._status = status;
            document.dispatchEvent(
                new CustomEvent("sk:notification-status", {
                    detail: { status },
                    bubbles: true,
                })
            );
            this._updateStatusIndicator();
        }

        _connect() {
            if (this._es) return;
            this._synced = false;
            this._pendingSyncIds = new Set();

            this._setStatus("connecting");
            let url = "/notifications/stream";
            if (this._lastTimestamp != null) {
                url += `?since=${this._lastTimestamp}`;
            }
            this._es = new EventSource(url);

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
                this._setStatus("reconnecting");
                setTimeout(() => this._connect(), 5000);
            };
        }

        _disconnect() {
            if (this._es) {
                this._es.close();
                this._es = null;
                this._setStatus("disconnected");
            }
        }

        _handleNotification(data) {
            if (data.type === "dismissed") {
                this._removeDismissed(data.id);
                return;
            }

            // Track latest timestamp for reconnect replay
            if (data.created_at != null && (this._lastTimestamp == null || data.created_at > this._lastTimestamp)) {
                this._lastTimestamp = data.created_at;
            }

            // Pre-sync: track IDs for deduplication
            if (!this._synced) {
                this._pendingSyncIds.add(data.id);
            }

            // Skip duplicates
            if (this._displayedIds.has(data.id)) return;

            // Group replacement: dismiss the previous notification in the same group
            if (data.group) {
                const oldId = this._groupMap.get(data.group);
                if (oldId) {
                    this._removeDismissed(oldId);
                }
                this._groupMap.set(data.group, data.id);
            }

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
            this._setStatus("connected");
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

        _ensureStatusIndicator() {
            if (this._statusIndicator) return this._statusIndicator;
            const el = document.createElement("div");
            el.className = "sk-status-indicator sk-status-indicator-hidden";
            el.innerHTML =
                '<span class="sk-status-dot"></span>' +
                '<span class="sk-status-label"></span>';
            document.body.appendChild(el);
            this._statusIndicator = el;
            return el;
        }

        _updateStatusIndicator() {
            const el = this._ensureStatusIndicator();
            const dot = el.querySelector(".sk-status-dot");
            const label = el.querySelector(".sk-status-label");

            clearTimeout(this._statusHideTimeout);

            const status = this._status;
            if (status === "connected") {
                clearTimeout(this._connectingTimeout);
                this._connectingTimeout = null;
                this._connectingSince = null;
                dot.style.backgroundColor = "var(--sk-color-success)";
                label.textContent = "Connected";
                el.classList.remove("sk-status-indicator-hidden");
                this._statusHideTimeout = setTimeout(() => {
                    el.classList.add("sk-status-indicator-hidden");
                }, 5000);
            } else if (status === "suspended") {
                clearTimeout(this._connectingTimeout);
                this._connectingTimeout = null;
                this._connectingSince = null;
                dot.style.backgroundColor = "var(--sk-color-muted)";
                label.textContent = "Paused";
                el.classList.remove("sk-status-indicator-hidden");
                this._statusHideTimeout = setTimeout(() => {
                    el.classList.add("sk-status-indicator-hidden");
                }, 5000);
            } else if (status === "connecting" || status === "reconnecting") {
                if (!this._connectingSince) {
                    this._connectingSince = Date.now();
                }
                el.classList.remove("sk-status-indicator-hidden");
                if (Date.now() - this._connectingSince >= 10000) {
                    dot.style.backgroundColor = "var(--sk-color-error)";
                    label.textContent = "Disconnected";
                } else {
                    dot.style.backgroundColor = "var(--sk-color-warning)";
                    label.textContent = "Connecting";
                    if (!this._connectingTimeout) {
                        const remaining = 10000 - (Date.now() - this._connectingSince);
                        this._connectingTimeout = setTimeout(() => {
                            this._connectingTimeout = null;
                            if (this._status === "connecting" || this._status === "reconnecting") {
                                dot.style.backgroundColor = "var(--sk-color-error)";
                                label.textContent = "Disconnected";
                            }
                        }, remaining);
                    }
                }
            } else {
                // disconnected (transient fallback)
                dot.style.backgroundColor = "var(--sk-color-error)";
                label.textContent = "Disconnected";
                el.classList.remove("sk-status-indicator-hidden");
            }
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

            const mode = data.mode || "queued";
            const { dismiss: dismissMode, autoClear } = this._getModeConfig(mode);

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

            if (dismissMode === "server" || dismissMode === "visual") {
                const dismiss = document.createElement("button");
                dismiss.type = "button";
                dismiss.className = "sk-notification-dismiss";
                dismiss.setAttribute("aria-label", "Dismiss");
                dismiss.innerHTML = "&times;";
                dismiss.addEventListener("click", () => {
                    if (dismissMode === "server") {
                        this._dismiss(data.id);
                    } else {
                        this._clearVisual(data.id);
                    }
                });
                article.appendChild(dismiss);
            }

            container.appendChild(article);

            if (autoClear) {
                article._autoClearTimer = setTimeout(
                    () => this._clearVisual(data.id),
                    autoClear,
                );
            }
        }

        _clearVisual(id) {
            const el = this._container?.querySelector(
                `[data-notification-id="${id}"]`
            );
            if (!el) return;

            clearTimeout(el._autoClearTimer);
            el.classList.add("sk-notification-exit");
            el.addEventListener("animationend", () => {
                el.remove();
                this._visibleCount--;
                this._displayedIds.delete(id);
                this._cleanGroupMap(id);
                this._showNextFromQueue();
            }, { once: true });
        }

        _dismiss(id) {
            const el = this._container?.querySelector(
                `[data-notification-id="${id}"]`
            );
            if (!el) return;

            clearTimeout(el._autoClearTimer);
            el.classList.add("sk-notification-exit");
            el.addEventListener("animationend", () => {
                el.remove();
                this._visibleCount--;
                this._displayedIds.delete(id);
                this._cleanGroupMap(id);
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
                this._cleanGroupMap(id);
                return;
            }

            clearTimeout(el._autoClearTimer);
            el.classList.add("sk-notification-exit");
            el.addEventListener("animationend", () => {
                el.remove();
                this._visibleCount--;
                this._displayedIds.delete(id);
                this._cleanGroupMap(id);
                this._showNextFromQueue();
            }, { once: true });
        }

        _cleanGroupMap(id) {
            for (const [group, gid] of this._groupMap) {
                if (gid === id) {
                    this._groupMap.delete(group);
                    break;
                }
            }
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
