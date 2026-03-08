/**
 * Skrift Service Worker — handles Web Push notification display
 * with client-side filtering support.
 *
 * Version is checked on each navigation; skipWaiting + clients.claim
 * ensure updates activate immediately without manual intervention.
 */

var SW_VERSION = 2;

// Activate immediately on install (don't wait for old SW to stop)
self.addEventListener("install", function (event) {
  self.skipWaiting();
});

// Claim all clients on activation (take control without reload)
self.addEventListener("activate", function (event) {
  event.waitUntil(self.clients.claim());
});

self.addEventListener("push", function (event) {
  if (!event.data) return;

  var data;
  try {
    data = event.data.json();
  } catch (e) {
    data = { title: "Notification", body: event.data.text() };
  }

  event.waitUntil(
    _filterAndShow(data)
  );
});

/**
 * Ask focused clients if this notification should be shown.
 * Clients can respond with:
 *   - { cancel: true } to suppress the notification
 *   - An object with updated fields (title, body, etc.)
 *   - No response (timeout) → show as-is
 */
function _filterAndShow(data) {
  return self.clients.matchAll({ type: "window", includeUncontrolled: false })
    .then(function (clientList) {
      // Find focused/visible clients
      var focusedClients = [];
      for (var i = 0; i < clientList.length; i++) {
        if (clientList[i].visibilityState === "visible") {
          focusedClients.push(clientList[i]);
        }
      }

      // No focused clients — show notification immediately
      if (focusedClients.length === 0) {
        return _showNotification(data);
      }

      // Ask focused clients to filter
      return _queryClients(focusedClients, data).then(function (result) {
        if (result && result.cancel) {
          return; // Client suppressed the notification
        }
        // Client may have modified the payload
        var finalData = result || data;
        return _showNotification(finalData);
      });
    });
}

/**
 * Post message to focused clients and wait for a response (200ms timeout).
 */
function _queryClients(focusedClients, data) {
  return new Promise(function (resolve) {
    var responded = false;
    var messageChannel = new MessageChannel();

    messageChannel.port1.onmessage = function (event) {
      if (!responded) {
        responded = true;
        resolve(event.data);
      }
    };

    // Send to the first focused client (most recently focused)
    focusedClients[0].postMessage(
      { type: "skrift-push-filter", payload: data },
      [messageChannel.port2]
    );

    // Timeout — show notification if no response
    setTimeout(function () {
      if (!responded) {
        responded = true;
        resolve(null);
      }
    }, 200);
  });
}

function _showNotification(data) {
  var title = data.title || "Notification";
  var options = {
    body: data.body || "",
    tag: data.tag || undefined,
    data: { url: data.url || "/" },
  };

  return self.registration.showNotification(title, options);
}

self.addEventListener("notificationclick", function (event) {
  event.notification.close();

  var url = event.notification.data && event.notification.data.url
    ? event.notification.data.url
    : "/";

  event.waitUntil(
    self.clients.matchAll({ type: "window", includeUncontrolled: true }).then(function (clientList) {
      // Focus existing window if possible
      for (var i = 0; i < clientList.length; i++) {
        var client = clientList[i];
        if (client.url.includes(url) && "focus" in client) {
          return client.focus();
        }
      }
      // Open new window
      if (clients.openWindow) {
        return clients.openWindow(url);
      }
    })
  );
});
