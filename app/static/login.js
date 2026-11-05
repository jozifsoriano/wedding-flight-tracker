document.getElementById("login-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const msg = document.getElementById("login-msg");
  msg.textContent = "Signing in…";
  try {
    const r = await fetch("/api/auth/login", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ password: document.getElementById("password").value }),
    });
    if (r.ok) return location.reload();
    msg.textContent = (await r.json().catch(() => ({}))).detail || "Sign-in failed";
  } catch { msg.textContent = "Could not reach the server"; }
});
