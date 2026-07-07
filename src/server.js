require('dotenv').config();
const path = require('path');
const express = require('express');
const { launchBrowser, closeBrowser, log, logError } = require('./grabber-core');
const { TabPool } = require('./session-manager');

const PORT = Number(process.env.PORT) || 3000;
const POOL_SIZE = Number(process.env.POOL_SIZE) || 2;
const PUBLIC_DIR = path.join(__dirname, '..', 'public');

let browser = null;
let tabPool = null;
let booting = false;

const app = express();
app.use(express.json());

app.get('/health', (req, res) => {
  if (!tabPool?.initialized) {
    res.status(503).json({ ok: false, poolReady: 0, booting });
    return;
  }

  res.json({
    ok: true,
    poolReady: tabPool.getReadyCount(),
    activeSessions: tabPool.sessions.size,
  });
});

app.post('/api/session', async (req, res) => {
  try {
    if (!tabPool?.initialized) {
      res.status(503).json({ error: 'Server is still starting — retry in a few seconds' });
      return;
    }

    const session = await tabPool.acquireSession();
    res.json(session);
  } catch (error) {
    logError('api', 'POST /api/session failed', error);
    res.status(503).json({ error: error.message });
  }
});

app.get('/api/session/:id/status', (req, res) => {
  const status = tabPool?.getStatus(req.params.id);
  if (!status) {
    res.status(404).json({ error: 'Session not found' });
    return;
  }

  res.json(status);
});

app.get('/api/session/:id/qr', (req, res) => {
  const qrBuffer = tabPool?.getQrBuffer(req.params.id);
  if (!qrBuffer) {
    res.status(404).json({ error: 'QR code not available for this session' });
    return;
  }

  res.set('Content-Type', 'image/png');
  res.set('Cache-Control', 'no-store');
  res.send(qrBuffer);
});

app.use(express.static(PUBLIC_DIR));

app.get('/', (req, res) => {
  res.sendFile(path.join(PUBLIC_DIR, 'index.html'));
});

async function bootBrowserPool() {
  if (booting || tabPool?.initialized) {
    return;
  }

  booting = true;
  try {
    log('server', 'Booting persistent browser and tab pool');
    browser = await launchBrowser();
    tabPool = new TabPool(browser, POOL_SIZE);
    await tabPool.init();
    log('server', `Tab pool online (size ${POOL_SIZE})`);
  } finally {
    booting = false;
  }
}

async function shutdown() {
  log('server', 'Shutting down');
  if (tabPool) {
    await tabPool.shutdown();
  }
  await closeBrowser(browser);
  process.exit(0);
}

process.on('SIGINT', shutdown);
process.on('SIGTERM', shutdown);

async function start() {
  app.listen(PORT, '0.0.0.0', () => {
    log('server', `Listening on http://0.0.0.0:${PORT}`);
  });

  bootBrowserPool().catch((error) => {
    logError('server', 'Failed to boot browser pool', error);
    process.exitCode = 1;
  });
}

start();
