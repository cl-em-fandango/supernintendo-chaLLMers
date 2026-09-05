import React from 'react';

// DownloadToast.jsx — the in-page confirmation shown after the fake
// download button is pressed. Renders inline (never window.alert) inside
// an aria-live region so screen readers announce the joke. `message` is
// plain data; dismissal calls `onDismiss`.
export default function DownloadToast({ message, onDismiss }) {
  return (
    <div className="hml-toast" role="status">
      <span className="hml-toast__sparkle" aria-hidden="true">
        ✦
      </span>
      <p className="hml-toast__message">{message}</p>
      <button
        type="button"
        className="hml-toast__dismiss"
        onClick={onDismiss}
      >
        Fine, I&apos;ll close it myself
      </button>
    </div>
  );
}
