const crypto = require('crypto');

// rawBody: Buffer con los bytes EXACTOS recibidos (no vuelvas a serializar el JSON).
function verify(secret, profile, tool, rawBody, tsHeader, sigHeader, now = Date.now() / 1000, maxSkew = 60) {
  if (typeof tsHeader !== 'string' || !/^(0|[1-9][0-9]*)$/.test(tsHeader)) return false;
  const ts = Number(tsHeader);
  if (Math.abs(now - ts) > maxSkew) return false;
  const sha = crypto.createHash('sha256').update(rawBody).digest('hex');
  const good = crypto.createHmac('sha256', secret).update(`${ts}.${profile}.${tool}.${sha}`).digest('hex');
  const a = Buffer.from(good), b = Buffer.from(String(sigHeader || ''));
  return a.length === b.length && crypto.timingSafeEqual(a, b);
}

module.exports = { verify };
