/**
 * Tests for skrift/static/js/notifications.js.
 *
 * The script is a browser IIFE with no module exports, so each test evaluates
 * the real source against a jsdom document and drives it through a stubbed
 * EventSource. Behaviour is asserted through the public surface — rendered
 * DOM, the `sk:notification` event, and `skrift:render` callbacks — rather
 * than by reaching for internals.
 */

import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

// Resolved from the project root: under the jsdom environment `import.meta.url`
// is an http URL, so it cannot be turned into a filesystem path.
const SOURCE = readFileSync(
    resolve(process.cwd(), "skrift/static/js/notifications.js"),
    "utf8",
);

/** Minimal EventSource stand-in — jsdom does not implement one. */
class FakeEventSource {
    static CONNECTING = 0;
    static OPEN = 1;
    static CLOSED = 2;

    constructor(url) {
        this.url = url;
        this.readyState = FakeEventSource.OPEN;
        this._listeners = {};
        FakeEventSource.last = this;
    }

    addEventListener(name, handler) {
        (this._listeners[name] ||= []).push(handler);
    }

    close() {
        this.readyState = FakeEventSource.CLOSED;
    }

    /** Deliver one SSE frame as the server would. */
    emit(name, data) {
        for (const handler of this._listeners[name] || []) {
            handler({ data: JSON.stringify(data) });
        }
    }
}

let client;
let errors;

/** Evaluate the client against the current document and return it. */
function loadClient() {
    new Function(SOURCE)();
    client = window.__skriftNotifications;
    return client;
}

/** Build a wire-format notification with sensible envelope defaults. */
function wire(overrides = {}) {
    return {
        type: "generic",
        id: "11111111-1111-4111-8111-111111111111",
        mode: "ephemeral",
        created_at: 1,
        payload: {},
        ...overrides,
    };
}

/** Load the client and push one notification through the SSE stream. */
function deliver(notification) {
    loadClient();
    FakeEventSource.last.emit("notification", notification);
    return notification;
}

beforeEach(() => {
    window.EventSource = FakeEventSource;
    errors = [];
    vi.spyOn(console, "error").mockImplementation((message) => errors.push(message));
});

afterEach(() => {
    // The client registers document/window listeners in its constructor and
    // has no teardown of its own; leaving them attached would let one test's
    // instance handle the next test's events.
    if (client) {
        document.removeEventListener("visibilitychange", client._onVisibilityChange);
        document.removeEventListener("sk:notification", client._onNotification);
        window.removeEventListener("focus", client._onFocus);
        window.removeEventListener("blur", client._onBlur);
        client._disconnect();
        client = null;
    }
    delete window.__skriftNotifications;
    document.body.innerHTML = "";
    vi.restoreAllMocks();
});

describe("generic toast rendering", () => {
    it("reads title and message from the nested payload", () => {
        deliver(wire({ payload: { title: "Saved", message: "Your draft was saved." } }));

        expect(document.querySelector(".sk-notification-title").textContent).toBe("Saved");
        expect(document.querySelector(".sk-notification-message").textContent).toBe(
            "Your draft was saved.",
        );
    });

    it("renders nothing for a payload without title or message", () => {
        deliver(wire());

        expect(document.querySelector(".sk-notification-title")).toBeNull();
        expect(document.querySelector(".sk-notification-message")).toBeNull();
    });

    it("uses the envelope id when the payload carries its own", () => {
        const n = deliver(
            wire({ payload: { title: "Hi", id: "email-42", type: "custom" } }),
        );

        const article = document.querySelector(".sk-notification");
        expect(article.dataset.notificationId).toBe(n.id);
    });

    it("uses the envelope mode when the payload carries its own", () => {
        deliver(
            wire({ mode: "queued", payload: { title: "Hi", mode: "ephemeral" } }),
        );

        // Queued notifications get a dismiss button; ephemeral ones do not.
        expect(document.querySelector(".sk-notification-dismiss")).not.toBeNull();
    });
});

describe("dismissed events", () => {
    it("removes the notification named by payload.notification_id", () => {
        const shown = deliver(wire({ payload: { title: "Hi" } }));
        expect(document.querySelector(".sk-notification")).not.toBeNull();

        FakeEventSource.last.emit(
            "notification",
            wire({
                type: "dismissed",
                id: "22222222-2222-4222-8222-222222222222",
                payload: { notification_id: shown.id },
            }),
        );

        const article = document.querySelector(".sk-notification");
        expect(article.classList.contains("sk-notification-exit")).toBe(true);
    });
});

describe("same id after a dismissal", () => {
    const id = "33333333-3333-4333-8333-333333333333";

    function dismiss() {
        FakeEventSource.last.emit(
            "notification",
            wire({
                type: "dismissed",
                id: "44444444-4444-4444-8444-444444444444",
                mode: "timeseries",
                payload: { notification_id: id, cleared_created_at: 1 },
            }),
        );
    }

    // An updated notification may reuse its id (same group, newer created_at),
    // and an app may resend an id after clearing it; both must be shown.
    it("shows a newer notification that reuses a dismissed id", () => {
        deliver(wire({ id, mode: "queued", group: "g", created_at: 1, payload: { title: "Old" } }));
        dismiss();
        // jsdom runs no animations; finish the exit the browser would.
        document.querySelector(".sk-notification").dispatchEvent(new Event("animationend"));
        FakeEventSource.last.emit(
            "notification",
            wire({ id, mode: "queued", group: "g", created_at: 2, payload: { title: "New" } }),
        );

        const titles = [...document.querySelectorAll(".sk-notification-title")].map((el) => el.textContent);
        expect(titles).toContain("New");
    });

    it("shows a resend of an id that was dismissed before it was ever shown", () => {
        loadClient();
        dismiss();
        FakeEventSource.last.emit(
            "notification",
            wire({ id, mode: "queued", created_at: 2, payload: { title: "Again" } }),
        );

        expect(document.querySelector(".sk-notification-title").textContent).toBe("Again");
    });
});

describe("clear dismissals carry the generation they cleared", () => {
    const id = "55555555-5555-4555-8555-555555555555";

    function clearFrame(clearedCreatedAt) {
        const payload = { notification_id: id };
        if (clearedCreatedAt !== undefined) payload.cleared_created_at = clearedCreatedAt;
        return wire({
            type: "dismissed",
            id: "66666666-6666-4666-8666-666666666666",
            mode: "timeseries",
            payload,
        });
    }

    function shown() {
        return document.querySelector(`[data-notification-id="${id}"]`);
    }

    // A clears the old row, its event is delayed, B delivers a fresh resend of
    // the same id, then A's event arrives naming the older generation.
    it("ignores a delayed clear of an older generation", () => {
        deliver(wire({ id, mode: "queued", created_at: 1800000000.000002, payload: { title: "Fresh" } }));
        FakeEventSource.last.emit("notification", clearFrame(1800000000.000001));

        expect(shown().classList.contains("sk-notification-exit")).toBe(false);
    });

    it("removes the generation it cleared", () => {
        deliver(wire({ id, mode: "queued", created_at: 1800000000.1234562, payload: { title: "Old" } }));
        // The cleared copy came back from storage rounded to the microsecond.
        FakeEventSource.last.emit("notification", clearFrame(1800000000.123456));

        expect(shown().classList.contains("sk-notification-exit")).toBe(true);
    });

    it("removes an older displayed generation", () => {
        deliver(wire({ id, mode: "queued", created_at: 1800000000.000001, payload: { title: "Old" } }));
        FakeEventSource.last.emit("notification", clearFrame(1800000000.000002));

        expect(shown().classList.contains("sk-notification-exit")).toBe(true);
    });

    it("still removes on a dismissal without the field", () => {
        deliver(wire({ id, mode: "queued", created_at: 1800000000.000002, payload: { title: "Fresh" } }));
        FakeEventSource.last.emit("notification", clearFrame());

        expect(shown().classList.contains("sk-notification-exit")).toBe(true);
    });
});

describe("sk:notification detail", () => {
    /** Deliver a notification and capture the object handed to listeners. */
    function capture(notification) {
        loadClient();
        let detail;
        document.addEventListener("sk:notification", (e) => {
            detail = e.detail;
        });
        FakeEventSource.last.emit("notification", notification);
        return detail;
    }

    it("exposes envelope fields and the nested payload", () => {
        const n = wire({ group: "deploy", payload: { title: "Hi" } });
        const detail = capture(n);

        expect(detail.id).toBe(n.id);
        expect(detail.type).toBe("generic");
        expect(detail.group).toBe("deploy");
        expect(detail.created_at).toBe(1);
        expect(detail.payload.title).toBe("Hi");
    });

    it("does not let payload keys shadow envelope fields", () => {
        const n = wire({
            type: "document.change",
            payload: {
                id: "email-42",
                type: "custom",
                mode: "nonsense",
                created_at: "yesterday",
                group: "payload-group",
            },
        });
        const detail = capture(n);

        expect(detail.id).toBe(n.id);
        expect(detail.type).toBe("document.change");
        expect(detail.mode).toBe("ephemeral");
        expect(detail.created_at).toBe(1);
        expect(detail.payload.id).toBe("email-42");
    });

    it("never forwards an envelope name the envelope itself omits", () => {
        // `group` is the one envelope field left out when unset, so a payload
        // entry named `group` would otherwise slip through the compatibility
        // path and shadow it after all.
        const detail = capture(wire({ payload: { group: "payload-group" } }));

        expect(detail.group).toBeUndefined();
        expect(errors).toHaveLength(1);
        expect(errors[0]).toContain("notification.payload.group");
    });

    it("prefers a present envelope field over a payload key of the same name", () => {
        const detail = capture(
            wire({ group: "envelope-group", payload: { group: "payload-group" } }),
        );

        expect(detail.group).toBe("envelope-group");
        expect(errors).toHaveLength(0);
    });

    it("forwards a payload key read off the envelope and names the fix", () => {
        const detail = capture(wire({ payload: { title: "Hi" } }));

        expect(detail.title).toBe("Hi");
        expect(errors).toHaveLength(1);
        expect(errors[0]).toContain("notification.payload.title");
    });

    it("reports each moved key once", () => {
        const detail = capture(wire({ payload: { title: "Hi", message: "Body" } }));

        detail.title;
        detail.title;
        expect(errors).toHaveLength(1);

        detail.message;
        expect(errors).toHaveLength(2);
    });

    it("returns undefined for a key absent everywhere, without reporting", () => {
        const detail = capture(wire({ payload: { title: "Hi" } }));

        expect(detail.nothingAnywhere).toBeUndefined();
        expect(errors).toHaveLength(0);
    });

    it("leaves feature detection on absent optional keys working", () => {
        const detail = capture(wire({ payload: { title: "Hi" } }));

        expect(detail.subtitle ? "shown" : "hidden").toBe("hidden");
        expect(errors).toHaveLength(0);
    });

    it("survives the property probes the runtime performs", async () => {
        const detail = capture(wire({ payload: { title: "Hi" } }));

        expect(JSON.parse(JSON.stringify(detail)).payload.title).toBe("Hi");
        expect(String(detail)).toBe("[object Object]");
        expect(typeof detail.hasOwnProperty).toBe("function");
        expect(await Promise.resolve(detail)).toBe(detail);
        expect(errors).toHaveLength(0);
    });

    it("suppresses the built-in toast when a listener calls preventDefault", () => {
        loadClient();
        document.addEventListener("sk:notification", (e) => e.preventDefault());
        FakeEventSource.last.emit("notification", wire({ payload: { title: "Hi" } }));

        expect(document.querySelector(".sk-notification")).toBeNull();
    });
});

describe("skrift:render watchers", () => {
    beforeEach(() => {
        document.body.innerHTML = `
            <div id="panel" skrift:watch-for="^document\\.change$"
                 skrift:render="App.render"></div>
        `;
    });

    afterEach(() => {
        delete window.App;
    });

    it("hands the renderer a notification whose payload is reachable", () => {
        let seen;
        window.App = {
            render(element, notification) {
                seen = notification;
                element.textContent = notification.payload.message;
            },
        };

        deliver(wire({ type: "document.change", payload: { message: "Changed" } }));

        expect(document.getElementById("panel").textContent).toBe("Changed");
        expect(seen.type).toBe("document.change");
        expect(errors).toHaveLength(0);
    });

    it("forwards and reports a payload key read off the envelope", () => {
        window.App = {
            render(element, notification) {
                element.textContent = notification.message;
            },
        };

        deliver(wire({ type: "document.change", payload: { message: "Changed" } }));

        expect(document.getElementById("panel").textContent).toBe("Changed");
        expect(errors).toHaveLength(1);
        expect(errors[0]).toContain("notification.payload.message");
    });
});
