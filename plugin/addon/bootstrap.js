/* Zotero bootstrap entry points of the translation plugin (Zotero 7-10). */

var chromeHandle;

async function startup({ id, rootURI }) {
  await Zotero.initializationPromise;
  const aomStartup = Cc["@mozilla.org/addons/addon-manager-startup;1"].getService(
    Ci.amIAddonManagerStartup,
  );
  chromeHandle = aomStartup.registerChrome(Services.io.newURI(rootURI + "manifest.json"), [
    ["content", "zotero-translate", rootURI + "content/"],
  ]);
  Services.scriptloader.loadSubScript(rootURI + "content/core.js");
  Services.scriptloader.loadSubScript(rootURI + "content/lifecycle.js");
  Services.scriptloader.loadSubScript(rootURI + "content/zotero-translate.js");
  ZoteroTranslate.startup({ id, rootURI });
}

function onMainWindowLoad({ window }) {
  ZoteroTranslate.addToWindow(window);
}

function onMainWindowUnload({ window }) {
  ZoteroTranslate.removeFromWindow(window);
}

function shutdown(data, reason) {
  if (reason === APP_SHUTDOWN) {
    return;
  }
  ZoteroTranslate.shutdown();
  chromeHandle.destruct();
  chromeHandle = null;
}

function install() {}

function uninstall() {}
