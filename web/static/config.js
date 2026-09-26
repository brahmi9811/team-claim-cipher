// Backend URL for the live view.
// "" = same origin (when web/api.py serves this page on localhost:8002).
// On Vercel, set this to the tunnel URL, or open the page with ?api=https://<tunnel> once
// (the choice is remembered in this browser).
window.CC_CONFIG = {
  API_URL: "",
};
