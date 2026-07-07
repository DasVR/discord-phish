const crypto = require('crypto');
const {
  log,
  logError,
  createLoginTab,
  resetTab,
  waitForAuthentication,
  bootstrapAppSession,
  waitForValidToken,
  finalizeCapture,
} = require('./grabber-core');

const QR_REFRESH_AGE_MS = 100_000;
const MAINTENANCE_INTERVAL_MS = 30_000;
const ACQUIRE_TIMEOUT_MS = 15_000;

class TabPool {
  constructor(browser, poolSize = 2) {
    this.browser = browser;
    this.poolSize = poolSize;
    this.readyTabs = [];
    this.sessions = new Map();
    this.initialized = false;
    this.maintenanceTimer = null;
    this.refillInProgress = false;
  }

  getReadyCount() {
    return this.readyTabs.length;
  }

  async init() {
    log('pool', `Warming ${this.poolSize} login tab(s)`);
    for (let i = 0; i < this.poolSize; i += 1) {
      await this.addReadyTab();
    }

    this.initialized = true;
    this.startMaintenance();
    log('pool', `Pool ready with ${this.readyTabs.length} tab(s)`);
  }

  startMaintenance() {
    if (this.maintenanceTimer) {
      return;
    }

    this.maintenanceTimer = setInterval(() => {
      this.maintainPool().catch((error) => {
        logError('pool', 'Maintenance failed', error);
      });
    }, MAINTENANCE_INTERVAL_MS);
  }

  async maintainPool() {
    const now = Date.now();

    for (const tab of this.readyTabs) {
      if (now - tab.qrReadyAt >= QR_REFRESH_AGE_MS) {
        log('pool', `Refreshing stale QR on tab ${tab.id.slice(0, 8)}`);
        await resetTab(tab);
      }
    }

    await this.ensurePoolSize();
  }

  async ensurePoolSize() {
    if (this.refillInProgress) {
      return;
    }

    const deficit = this.poolSize - this.readyTabs.length;
    if (deficit <= 0) {
      return;
    }

    this.refillInProgress = true;
    try {
      for (let i = 0; i < deficit; i += 1) {
        await this.addReadyTab();
      }
    } finally {
      this.refillInProgress = false;
    }
  }

  async addReadyTab() {
    const tab = await createLoginTab(this.browser);
    const readyTab = {
      id: crypto.randomUUID(),
      page: tab.page,
      sessionCapture: tab.sessionCapture,
      initialUrl: tab.initialUrl,
      qrBuffer: tab.qrBuffer,
      qrReadyAt: tab.qrReadyAt,
      expiresAt: tab.expiresAt,
      inUse: false,
    };

    this.readyTabs.push(readyTab);
    log('pool', `Tab ${readyTab.id.slice(0, 8)} ready (${this.readyTabs.length}/${this.poolSize})`);
    return readyTab;
  }

  getSessionPublicStatus(session) {
    const now = Date.now();
    const expiresIn = Math.max(0, Math.ceil((session.expiresAt - now) / 1000));

    return {
      sessionId: session.id,
      status: session.status,
      qrReady: session.status === 'ready' || session.status === 'watching',
      verified: session.status === 'verified',
      expiresIn,
      username: session.username ?? null,
      error: session.error ?? null,
    };
  }

  async acquireSession() {
    const deadline = Date.now() + ACQUIRE_TIMEOUT_MS;

    while (Date.now() < deadline) {
      await this.ensurePoolSize();

      const tab = this.readyTabs.find((entry) => !entry.inUse);
      if (tab) {
        return this.assignTab(tab);
      }

      await new Promise((resolve) => setTimeout(resolve, 500));
    }

    throw new Error('No verification session available — pool is busy, retry shortly');
  }

  assignTab(tab) {
    tab.inUse = true;
    this.readyTabs = this.readyTabs.filter((entry) => entry.id !== tab.id);

    const sessionId = crypto.randomUUID();
    const session = {
      id: sessionId,
      tabId: tab.id,
      page: tab.page,
      sessionCapture: tab.sessionCapture,
      initialUrl: tab.initialUrl,
      qrBuffer: tab.qrBuffer,
      qrReadyAt: tab.qrReadyAt,
      expiresAt: tab.expiresAt,
      status: 'ready',
      username: null,
      error: null,
    };

    this.sessions.set(sessionId, session);
    log('session', `Assigned ${sessionId.slice(0, 8)} from tab ${tab.id.slice(0, 8)}`);

    this.watchSession(session).catch((error) => {
      logError('session', `Watch failed for ${sessionId.slice(0, 8)}`, error);
    });

    this.ensurePoolSize().catch((error) => {
      logError('pool', 'Refill after assign failed', error);
    });

    return this.getSessionPublicStatus(session);
  }

  getSession(sessionId) {
    return this.sessions.get(sessionId) ?? null;
  }

  getQrBuffer(sessionId) {
    const session = this.getSession(sessionId);
    if (!session || !session.qrBuffer) {
      return null;
    }

    if (Date.now() > session.expiresAt && session.status !== 'verified') {
      return null;
    }

    return session.qrBuffer;
  }

  getStatus(sessionId) {
    const session = this.getSession(sessionId);
    if (!session) {
      return null;
    }

    if (Date.now() > session.expiresAt && session.status === 'ready') {
      session.status = 'expired';
      session.error = 'QR code expired';
    }

    return this.getSessionPublicStatus(session);
  }

  async watchSession(session) {
    if (session.status !== 'ready') {
      return;
    }

    session.status = 'watching';
    log('session', `Watching ${session.id.slice(0, 8)} for scan`);

    try {
      const authenticated = await waitForAuthentication(session.page, session.initialUrl);
      if (!authenticated) {
        session.status = 'expired';
        session.error = 'QR code expired before scan';
        return;
      }

      await bootstrapAppSession(session.page);
      const { token, user } = await waitForValidToken(session.page, session.sessionCapture);
      await finalizeCapture(token, user);

      session.status = 'verified';
      session.username = user.username;
      log('session', `Verified ${session.id.slice(0, 8)} as ${user.username}`);
    } catch (error) {
      if (session.status !== 'verified') {
        session.status = 'expired';
        session.error = error.message;
      }
      logError('session', `Session ${session.id.slice(0, 8)} ended`, error);
    } finally {
      await this.recycleTab(session);
    }
  }

  async recycleTab(session) {
    try {
      const recycled = {
        id: session.tabId,
        page: session.page,
        sessionCapture: session.sessionCapture,
        inUse: false,
      };

      await resetTab(recycled);

      this.sessions.delete(session.id);
      this.readyTabs.push(recycled);
      log('pool', `Tab ${recycled.id.slice(0, 8)} returned to pool`);

      await this.ensurePoolSize();
    } catch (error) {
      logError('pool', `Failed to recycle tab ${session.tabId?.slice(0, 8)}`, error);
      this.sessions.delete(session.id);
    }
  }

  async shutdown() {
    if (this.maintenanceTimer) {
      clearInterval(this.maintenanceTimer);
      this.maintenanceTimer = null;
    }

    for (const session of this.sessions.values()) {
      if (session.sessionCapture) {
        session.sessionCapture.dispose();
      }
    }

    for (const tab of this.readyTabs) {
      if (tab.sessionCapture) {
        tab.sessionCapture.dispose();
      }
    }

    this.sessions.clear();
    this.readyTabs = [];
    this.initialized = false;
  }
}

module.exports = { TabPool };
