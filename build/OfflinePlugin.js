const crypto = require('crypto')
const fs = require('fs')
const path = require('path')

// Run after HTML, CSS, copied files and every lazy chunk have been emitted.
class OfflinePlugin {
  apply (compiler) {
    compiler.hooks.thisCompilation.tap('OfflinePlugin', compilation => {
      compilation.hooks.processAssets.tap({
        name: 'OfflinePlugin',
        stage: compiler.webpack.Compilation.PROCESS_ASSETS_STAGE_REPORT
      }, () => {
        if (compilation.errors.length) return
        const digest = data => crypto.createHash('sha256').update(data).digest('hex')
        const template = fs.readFileSync(path.join(__dirname, '../src/offline/service-worker.js'), 'utf8')
        const assets = compilation.getAssets()
          .filter(asset => !/\.(map|zip)$/.test(asset.name))
          .map(asset => ({ url: asset.name, hash: digest(asset.source.buffer()) }))
          .sort((a, b) => a.url.localeCompare(b.url))
        const version = digest(JSON.stringify(assets) + template).slice(0, 20)
        const build = JSON.stringify({ version, assets })
        compilation.emitAsset('service-worker.js', new compiler.webpack.sources.RawSource(template.replace('const BUILD = __OFFLINE_BUILD__', `const BUILD = ${build}`)))
      })
    })
  }
}
module.exports = OfflinePlugin
