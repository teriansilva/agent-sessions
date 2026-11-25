import { useCallback, useEffect, useState } from "react";
import { api } from "../../lib/api";
import type { PushSubscriptionInfo } from "../../types/api";
import styles from "../../routes/Settings.module.css";

/** Registering THIS browser for Web Push (#726 Phase 3).
 *
 *  Without this the push half of the feature is unreachable: the server can encrypt and send,
 *  but no device has ever handed it an endpoint to send to. The in-app bell works regardless
 *  (it polls) — push is what reaches the operator when the tab is closed, which is the case
 *  that matters at 2am.
 *
 *  Three constraints shape this component:
 *
 *  - **`Notification.requestPermission()` must be called from a user gesture.** Browsers reject
 *    it otherwise, and Safari silently. So this is a button, never an effect.
 *  - **Permission is not revocable from script.** Once a user picks "Block" the app cannot
 *    re-prompt; only browser settings can undo it. Saying so plainly beats a button that
 *    silently does nothing.
 *  - **The endpoint is a capability.** Anyone holding the full URL can push to that device, so
 *    the server only ever returns an id + origin, and that's all we render.
 */
/** This browser's device id, derived exactly as the server derives it: the first 16 hex chars
 *  of SHA-256 over the endpoint. The server never returns the endpoint (it is a capability
 *  anyone holding it could push with), so matching the id is the only way the client can tell
 *  which row in the list is itself. Returns "" where WebCrypto is unavailable, which makes the
 *  caller fail SAFE — it skips the local unsubscribe rather than guessing. */
async function localDeviceId(endpoint: string): Promise<string> {
  const subtle = globalThis.crypto?.subtle;
  if (!subtle) return "";
  const digest = await subtle.digest(
    "SHA-256",
    new TextEncoder().encode(endpoint),
  );
  return Array.from(new Uint8Array(digest))
    .map((b) => b.toString(16).padStart(2, "0"))
    .join("")
    .slice(0, 16);
}

export function PushDevices() {
  const [devices, setDevices] = useState<PushSubscriptionInfo[]>([]);
  const [supported] = useState(
    () =>
      // Truthiness, not `in`: `"PushManager" in window` is true for a key present but set to
      // undefined, which is exactly the shape a polyfill-shimmed or partially-supported
      // browser presents — and it would send us down the happy path into a TypeError.
      typeof window !== "undefined" &&
      typeof window.PushManager !== "undefined" &&
      typeof window.Notification !== "undefined",
  );
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");
  const [permission, setPermission] = useState<string>(() =>
    typeof Notification === "undefined" ? "default" : Notification.permission,
  );

  const refresh = useCallback(() => {
    Promise.resolve()
      .then(() => api.pushKey())
      .then((r) => setDevices(r.subscriptions ?? []))
      .catch(() => setDevices([]));
  }, []);

  useEffect(() => {
    if (supported) refresh();
  }, [supported, refresh]);

  const enable = async () => {
    setBusy(true);
    setErr("");
    try {
      const perm = await Notification.requestPermission();
      setPermission(perm);
      if (perm !== "granted") {
        setErr(
          perm === "denied"
            ? "Notifications are blocked for this site. Only your browser's settings can undo that — the app can't ask again."
            : "Permission wasn't granted.",
        );
        return;
      }
      const reg = await navigator.serviceWorker.ready;
      const { public_key: key } = await api.pushKey();
      if (!key) {
        setErr("The server has no push key configured yet.");
        return;
      }
      // An existing subscription is reused rather than replaced: re-subscribing would mint a
      // new endpoint and orphan the old row server-side.
      const sub =
        (await reg.pushManager.getSubscription()) ??
        (await reg.pushManager.subscribe({
          userVisibleOnly: true,
          applicationServerKey: key,
        }));
      await api.pushSubscribe(sub.toJSON());
      refresh();
    } catch (e) {
      setErr(
        e instanceof Error
          ? e.message
          : "Could not enable push on this device.",
      );
    } finally {
      setBusy(false);
    }
  };

  const remove = async (id: string) => {
    setBusy(true);
    setErr("");
    try {
      await api.pushUnsubscribe(id);
      // Only tear down the LOCAL subscription when the row being removed IS this browser.
      // This used to unsubscribe unconditionally, so removing a phone from the desktop killed
      // desktop push too — and left the desktop's server row behind, pushing to an endpoint
      // the browser had already dropped. The device list is opaque ids, so the only way to
      // know is to derive this browser's own id the same way the server does.
      const reg = await navigator.serviceWorker.ready;
      const sub = await reg.pushManager.getSubscription();
      if (sub && (await localDeviceId(sub.endpoint)) === id) {
        await sub.unsubscribe();
      }
      refresh();
    } catch (e) {
      setErr(e instanceof Error ? e.message : "Could not remove that device.");
    } finally {
      setBusy(false);
    }
  };

  if (!supported) {
    return (
      <div className={styles.aiField}>
        <span className={styles.aiFieldLabel}>Push notifications</span>
        <p className={styles.hint}>
          This browser doesn't support Web Push. The notification bell still
          works — it polls. On iOS, push requires adding the app to your home
          screen first.
        </p>
      </div>
    );
  }

  return (
    <div className={styles.aiField}>
      <span className={styles.aiFieldLabel}>Push notifications</span>
      <p className={styles.hint}>
        Wakes you when the tab is closed. The bell works without this.
      </p>
      <div className={styles.aiActions}>
        <button
          type="button"
          className={styles.secBtn}
          onClick={() => void enable()}
          disabled={busy || permission === "denied"}
        >
          {busy ? "Working…" : "Enable on this device"}
        </button>
      </div>
      {err && <p className={styles.err}>{err}</p>}
      {devices.length > 0 && (
        <ul className={styles.hint}>
          {devices.map((d) => (
            <li key={d.id}>
              {d.origin || "unknown device"}{" "}
              <button
                type="button"
                className={styles.secBtn}
                onClick={() => void remove(d.id)}
                disabled={busy}
                aria-label={`Remove ${d.origin || "device"}`}
              >
                Remove
              </button>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
