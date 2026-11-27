// Return page for a sign-in made at Google, behind the entrance for several customers.
// The browser arrives here from another site, so the session cookie does not come along. This page
// reads nothing and stores nothing: it moves Google's answer to the part after «#», which is never
// sent to a server, and goes on to the panel. That step stays inside this site, so the session
// travels again and the panel hands the answer over with its own token.
const read = text => new URLSearchParams(text.slice(1));
const inAddress = read(location.search);
// Google documents the answer in the address; one that came after «#» is carried on the same way.
const answer = inAddress.has('state') ? inAddress : read(location.hash);
// Only the three values of the answer go on, and only in the form they can have.
const allowed = {state: /^gmail-[A-Za-z0-9_-]{20,200}$/, code: /^[\x21-\x7e]{1,4096}$/, error: /^[a-z_]{1,64}$/};
const kept = new URLSearchParams();
for (const [name, form] of Object.entries(allowed)) {
  const value = answer.get(name) || '';
  if (form.test(value)) kept.set(name, value);
}
const complete = kept.has('state') && (kept.has('code') || kept.has('error'));
location.replace('/#' + (complete ? kept.toString() : 'ajustes'));
