const { app, BrowserWindow, Menu, ipcMain } = require('electron');
const path = require('path');
const isDev = require('electron-is-dev');
const { spawn } = require('child_process');
const net = require('net');
const fs = require('fs');

let mainWindow;
let platformProcess;
let hybridProcess;
let isShuttingDown = false;

// Platform & Hybrid server status
const PLATFORM_PORT = 8080;
const HYBRID_PORT = 8000;

function isPortAvailable(port) {
  return new Promise((resolve) => {
    const server = net.createServer();
    server.once('error', () => resolve(false));
    server.once('listening', () => {
      server.close();
      resolve(true);
    });
    server.listen(port);
  });
}

function sleep(ms) {
  return new Promise(resolve => setTimeout(resolve, ms));
}

async function waitForServer(port, maxAttempts = 30) {
  for (let i = 0; i < maxAttempts; i++) {
    try {
      const response = await fetch(`http://localhost:${port}/health`, { timeout: 2000 });
      if (response.ok) return true;
    } catch (e) {
      // Server not ready yet
    }
    await sleep(1000);
  }
  return false;
}

function startServers() {
  return new Promise(async (resolve, reject) => {
    const repoRoot = path.join(__dirname, '../../..');
    console.log('Repo root:', repoRoot);

    // Start Platform Backend
    console.log('[Electron] Starting Platform Backend on port', PLATFORM_PORT);
    platformProcess = spawn('python', [
      path.join(repoRoot, 'platform/backend/main.py')
    ], {
      cwd: repoRoot,
      stdio: 'pipe',
      shell: true // Windows compatibility
    });

    platformProcess.stdout?.on('data', (data) => {
      console.log(`[Platform] ${data}`);
    });
    platformProcess.stderr?.on('data', (data) => {
      console.error(`[Platform Error] ${data}`);
    });

    // Start Hybrid Engine
    console.log('[Electron] Starting Hybrid Engine on port', HYBRID_PORT);
    hybridProcess = spawn('python', [
      path.join(repoRoot, 'Hybrid/src_python/app.py')
    ], {
      cwd: repoRoot,
      stdio: 'pipe',
      shell: true
    });

    hybridProcess.stdout?.on('data', (data) => {
      console.log(`[Hybrid] ${data}`);
    });
    hybridProcess.stderr?.on('data', (data) => {
      console.error(`[Hybrid Error] ${data}`);
    });

    // Wait for servers to be ready
    console.log('[Electron] Waiting for servers to start...');
    const platformReady = await waitForServer(PLATFORM_PORT);
    const hybridReady = await waitForServer(HYBRID_PORT);

    if (platformReady && hybridReady) {
      console.log('[Electron] Both servers are ready!');
      resolve();
    } else {
      console.error('[Electron] Servers failed to start');
      reject(new Error('Failed to start backend servers'));
    }
  });
}

function createWindow() {
  mainWindow = new BrowserWindow({
    width: 1600,
    height: 900,
    minWidth: 1200,
    minHeight: 700,
    webPreferences: {
      nodeIntegration: false,
      contextIsolation: true,
      preload: path.join(__dirname, 'preload.js')
    },
    icon: path.join(__dirname, 'icon.png') // Optional: add an icon
  });

  const startUrl = isDev
    ? 'http://localhost:3000'
    : `file://${path.join(__dirname, '../build/index.html')}`;

  console.log('[Electron] Loading URL:', startUrl);
  mainWindow.loadURL(startUrl);

  if (isDev) {
    mainWindow.webContents.openDevTools();
  }

  mainWindow.on('closed', () => {
    mainWindow = null;
  });
}

app.on('ready', async () => {
  try {
    await startServers();
    createWindow();
    createMenu();
  } catch (error) {
    console.error('Failed to start app:', error);
    app.quit();
  }
});

app.on('window-all-closed', () => {
  if (process.platform !== 'darwin') {
    app.quit();
  }
});

app.on('activate', () => {
  if (mainWindow === null) {
    createWindow();
  }
});

app.on('before-quit', () => {
  isShuttingDown = true;
  if (platformProcess) {
    console.log('[Electron] Killing Platform process');
    platformProcess.kill();
  }
  if (hybridProcess) {
    console.log('[Electron] Killing Hybrid process');
    hybridProcess.kill();
  }
});

function createMenu() {
  const template = [
    {
      label: 'File',
      submenu: [
        {
          label: 'Exit',
          accelerator: 'CmdOrCtrl+Q',
          click: () => {
            app.quit();
          }
        }
      ]
    },
    {
      label: 'View',
      submenu: [
        {
          label: 'Reload',
          accelerator: 'CmdOrCtrl+R',
          click: () => {
            if (mainWindow) mainWindow.reload();
          }
        },
        {
          label: 'Toggle Developer Tools',
          accelerator: 'CmdOrCtrl+Shift+I',
          click: () => {
            if (mainWindow) mainWindow.webContents.toggleDevTools();
          }
        }
      ]
    }
  ];

  const menu = Menu.buildFromTemplate(template);
  Menu.setApplicationMenu(menu);
}

// IPC handlers
ipcMain.handle('get-backend-urls', () => {
  return {
    platformApi: `http://localhost:${PLATFORM_PORT}/api/v1`,
    engineApi: `http://localhost:${HYBRID_PORT}`
  };
});
