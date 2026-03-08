/**
 * SkriftPush — Web Push subscription management and client-side filtering.
 *
 * Fetches the VAPID public key, subscribes via the Push API,
 * and sends the subscription to the server.
 *
 * Push filter:
 *   window.__skriftPush.onFilter(function(payload) {
 *     if (payload.tag === "chat:123" && isViewingChat("123")) {
 *       return { cancel: true };  // suppress notification
 *     }
 *     return payload;  // show as-is (or modify fields)
 *   });
 */
(function () {
  "use strict";

  var SkriftPush = {
    _subscribed: false,
    _filterCallback: null,

    async subscribe() {
      if (!("serviceWorker" in navigator) || !("PushManager" in window)) {
        console.warn("Push notifications not supported");
        return false;
      }

      try {
        var registration = await navigator.serviceWorker.ready;

        // Fetch VAPID public key
        var res = await fetch("/push/vapid-key");
        if (!res.ok) return false;
        var data = await res.json();
        var publicKey = data.publicKey;

        // Convert base64url to Uint8Array
        var key = publicKey.replace(/-/g, "+").replace(/_/g, "/");
        var padding = "=".repeat((4 - (key.length % 4)) % 4);
        var raw = atob(key + padding);
        var applicationServerKey = new Uint8Array(raw.length);
        for (var i = 0; i < raw.length; i++) {
          applicationServerKey[i] = raw.charCodeAt(i);
        }

        var subscription = await registration.pushManager.subscribe({
          userVisibleOnly: true,
          applicationServerKey: applicationServerKey,
        });

        // Send subscription to server
        var subRes = await fetch("/push/subscribe", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(subscription.toJSON()),
        });

        this._subscribed = subRes.ok;
        return this._subscribed;
      } catch (err) {
        console.error("Push subscription failed:", err);
        return false;
      }
    },

    async unsubscribe() {
      if (!("serviceWorker" in navigator)) return false;

      try {
        var registration = await navigator.serviceWorker.ready;
        var subscription = await registration.pushManager.getSubscription();
        if (!subscription) return true;

        await fetch("/push/unsubscribe", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ endpoint: subscription.endpoint }),
        });

        await subscription.unsubscribe();
        this._subscribed = false;
        return true;
      } catch (err) {
        console.error("Push unsubscribe failed:", err);
        return false;
      }
    },

    async isSubscribed() {
      if (!("serviceWorker" in navigator) || !("PushManager" in window)) {
        return false;
      }
      try {
        var registration = await navigator.serviceWorker.ready;
        var subscription = await registration.pushManager.getSubscription();
        return subscription !== null;
      } catch {
        return false;
      }
    },

    /**
     * Register a filter callback for incoming push notifications.
     *
     * The callback receives the push payload and should return:
     *   - { cancel: true } to suppress the notification
     *   - A modified payload object to update the notification
     *   - The original payload (or nothing) to show as-is
     *
     * @param {Function} callback - function(payload) => payload | {cancel: true}
     */
    onFilter(callback) {
      this._filterCallback = callback;
    },
  };

  // Listen for filter requests from the service worker
  if ("serviceWorker" in navigator) {
    navigator.serviceWorker.addEventListener("message", function (event) {
      if (!event.data || event.data.type !== "skrift-push-filter") return;
      if (!event.ports || !event.ports[0]) return;

      var payload = event.data.payload;
      var result = payload;

      if (SkriftPush._filterCallback) {
        try {
          result = SkriftPush._filterCallback(payload) || payload;
        } catch (err) {
          console.error("Push filter error:", err);
          result = payload;
        }
      }

      event.ports[0].postMessage(result);
    });
  }

  window.__skriftPush = SkriftPush;
})();
