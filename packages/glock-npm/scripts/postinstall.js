#!/usr/bin/env node

/**
 * Glock postinstall script
 *
 * Downloads the appropriate platform binary after npm install.
 */

const https = require('https');
const fs = require('fs');
const path = require('path');
const os = require('os');
const { execSync } = require('child_process');

const VERSION = require('../../package.json').version;
const PLATFORM = os.platform();
const ARCH = os.arch();

// Map to our binary naming convention
const platformMap = {
  'darwin': 'darwin',
  'linux': 'linux',
  'win32': 'win32',
};

const archMap = {
  'x64': 'x64',
  'arm64': 'arm64',
};

const mappedPlatform = platformMap[PLATFORM];
const mappedArch = archMap[ARCH];

if (!mappedPlatform || !mappedArch) {
  console.error(`Unsupported platform: ${PLATFORM}-${ARCH}`);
  console.error('Glock supports: darwin-x64, darwin-arm64, linux-x64, linux-arm64, win32-x64');
  process.exit(1);
}

const BINARY_NAME = PLATFORM === 'win32' ? 'glock.exe' : 'glock';
const DOWNLOAD_BASE = 'https://github.com/glock/glock/releases/download';

async function downloadBinary() {
  const url = `${DOWNLOAD_BASE}/v${VERSION}/glock-${mappedPlatform}-${mappedArch}.tar.gz`;
  const binDir = path.join(__dirname, '..', 'bin', `glock-${mappedPlatform}-${mappedArch}`);
  const tarPath = path.join(binDir, 'glock.tar.gz');

  console.log(`Downloading Glock v${VERSION} for ${mappedPlatform}-${mappedArch}...`);

  // Create directory
  fs.mkdirSync(binDir, { recursive: true });

  // Download file
  await new Promise((resolve, reject) => {
    const file = fs.createWriteStream(tarPath);

    const request = (downloadUrl) => {
      https.get(downloadUrl, (response) => {
        // Handle redirects
        if (response.statusCode === 301 || response.statusCode === 302) {
          const redirectUrl = response.headers.location;
          if (redirectUrl) {
            request(redirectUrl);
            return;
          }
        }

        if (response.statusCode !== 200) {
          reject(new Error(`Download failed: HTTP ${response.statusCode}`));
          return;
        }

        response.pipe(file);
        file.on('finish', () => {
          file.close();
          resolve();
        });
      }).on('error', (err) => {
        fs.unlink(tarPath, () => {});
        reject(err);
      });
    };

    request(url);
  });

  // Extract archive
  console.log('Extracting...');
  try {
    execSync(`tar -xzf glock.tar.gz`, { cwd: binDir, stdio: 'pipe' });
  } catch (err) {
    // Try with gzip separately on some systems
    try {
      execSync(`gzip -d glock.tar.gz && tar -xf glock.tar`, { cwd: binDir, stdio: 'pipe' });
    } catch (err2) {
      throw new Error('Failed to extract archive. Please ensure tar is available.');
    }
  }

  // Clean up archive
  try {
    fs.unlinkSync(tarPath);
  } catch (e) {
    // Ignore cleanup errors
  }
  try {
    fs.unlinkSync(path.join(binDir, 'glock.tar'));
  } catch (e) {
    // Ignore cleanup errors
  }

  // Make executable on Unix
  if (PLATFORM !== 'win32') {
    const binaryPath = path.join(binDir, BINARY_NAME);
    fs.chmodSync(binaryPath, 0o755);
  }

  console.log('Glock installed successfully!');
  console.log('');
  console.log('Run `glock` to start an interactive session.');
}

// Only run if not in CI or if explicitly requested
const skipDownload = process.env.GLOCK_SKIP_BINARY_DOWNLOAD === '1';
const isCI = process.env.CI === 'true';

if (skipDownload) {
  console.log('Skipping binary download (GLOCK_SKIP_BINARY_DOWNLOAD=1)');
  process.exit(0);
}

downloadBinary().catch((err) => {
  console.error('');
  console.error('Failed to download Glock binary:', err.message);
  console.error('');
  console.error('You can try:');
  console.error('  1. Check your network connection');
  console.error('  2. Download manually from https://github.com/glock/glock/releases');
  console.error('  3. Build from source');
  console.error('');

  // Don't fail the install - the binary might be added manually
  if (!isCI) {
    process.exit(0);
  }
  process.exit(1);
});
