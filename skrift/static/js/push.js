/**
 * SkriftPush — Web Push subscription management.
 *
 * Fetches the VAPID public key, subscribes via the Push API,
 * and sends the subscription to the server.
 */
(function () {
  "use strict";

  var SkriftPush = {
    _subscribed: false,

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
  };

  window.__skriftPush = SkriftPush;
})();
