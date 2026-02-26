#!/usr/bin/env node

/**
 * Glock CLI launcher
 *
 * This script launches the platform-appropriate Glock binary.
 * The binary is downloaded during npm postinstall.
 */

const { spawn } = require('child_process');
const path = require('path');
const os = require('os');
const fs = require('fs');

const platform = os.platform();
const arch = os.arch();

// Map Node.js arch names to our binary names
const archMap = {
  'x64': 'x64',
  'arm64': 'arm64',
};

const platformMap = {
  'darwin': 'darwin',
  'linux': 'linux',
  'win32': 'win32',
};

const mappedPlatform = platformMap[platform];
const mappedArch = archMap[arch];

if (!mappedPlatform || !mappedArch) {
  console.error(`Unsupported platform: ${platform}-${arch}`);
  process.exit(1);
}

const binaryName = platform === 'win32' ? 'glock.exe' : 'glock';
const binaryDir = path.join(__dirname, '..', 'bin', `glock-${mappedPlatform}-${mappedArch}`);
const binaryPath = path.join(binaryDir, binaryName);

// Check if binary exists
if (!fs.existsSync(binaryPath)) {
  console.error('Glock binary not found. Please reinstall the package.');
  console.error(`Expected binary at: ${binaryPath}`);
  console.error('');
  console.error('Try running: npm install -g glock/glock');
  process.exit(1);
}

// Spawn the binary with all arguments
const child = spawn(binaryPath, process.argv.slice(2), {
  stdio: 'inherit',
  env: {
    ...process.env,
    GLOCK_NPM_INSTALL: '1',
  },
});

child.on('error', (err) => {
  console.error(`Failed to start Glock: ${err.message}`);
  process.exit(1);
});

child.on('exit', (code, signal) => {
  if (signal) {
    process.exit(1);
  }
  process.exit(code || 0);
});

// Handle signals
process.on('SIGINT', () => {
  child.kill('SIGINT');
});

process.on('SIGTERM', () => {
  child.kill('SIGTERM');
});
