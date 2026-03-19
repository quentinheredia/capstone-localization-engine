const { contextBridge, ipcMain } = require('electron');

// Expose safe APIs to the renderer
contextBridge.exposeInMainWorld('electron', {
  getBackendUrls: () => ipcRenderer.invoke('get-backend-urls')
});
