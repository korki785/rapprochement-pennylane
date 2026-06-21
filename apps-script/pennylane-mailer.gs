/**
 * Pennylane Mailer - Google Apps Script (version ASCII, sans accents).
 * Envoie vers Pennylane les factures recues par email (y compris dans le CORPS du mail).
 * Aucun filtre Gmail a configurer : le script cherche lui-meme les emails-factures.
 *
 * Deux filets :
 *   1) SENDERS     : liste blanche d'emetteurs connus (capte meme les recus sans PDF).
 *   2) heuristique : emetteur "automatique" (noreply/billing/facture...) + PDF joint
 *                    + mot facture/invoice/recu dans l'objet + pas une reponse.
 *
 * A deployer sur CHAQUE compte Gmail (voir README.md).
 */

// ====== A PERSONNALISER ======
var PENNYLANE_ADDRESS = 'maison-darwish-ebm54v7e@suppliers.pennylane.com';
var SINCE = '2026/03/31';
var SENT_LABEL = 'Pennylane-Sent';
var MAX_THREADS_PER_RUN = 40;

var SENDERS = [
  'messaging.squareup.com', 'sent-via.netsuite.com', 'mail.anthropic.com', 'sundayapp.io',
  'payments-noreply@google.com', 'alan.eu', 'compta@kandbaz.com', 'message-service@sender.zohobooks.com',
  'bouygues-telecom.fr', 'spoton.com', 'toasttab.com', 'clover.com', 'bypassmobile.com',
  'info.email.aa.com', 'accor.com', 'notification.transavia.com',
  'uber.com', 'openai.com', 'hostinger.com', 'wix.com'
];
var AUTO_SENDER_HINTS = ['noreply','no-reply','nepasrepondre','donotreply','billing','invoice','invoices','facture','facturation','receipt','receipts','statements'];
var SUBJECT_KEYWORDS = ['facture','invoice','receipt','recu'];
var INVOICE_ATTACHMENT_TYPES = ['application/pdf','image/jpeg','image/jpg','image/png'];
// =============================

function buildAllowlistQuery_() {
  return 'from:(' + SENDERS.join(' OR ') + ') after:' + SINCE + ' -from:me -label:' + SENT_LABEL;
}
function buildHeuristicQuery_() {
  return 'from:(' + AUTO_SENDER_HINTS.join(' OR ') + ') subject:(' + SUBJECT_KEYWORDS.join(' OR ') +
         ') has:attachment filename:pdf -subject:(re OR fwd OR tr OR sv) after:' + SINCE + ' -from:me -label:' + SENT_LABEL;
}
function gatherThreads_() {
  var seen = {}, out = [];
  [buildAllowlistQuery_(), buildHeuristicQuery_()].forEach(function (q) {
    GmailApp.search(q, 0, MAX_THREADS_PER_RUN).forEach(function (t) {
      var id = t.getId(); if (!seen[id]) { seen[id] = true; out.push(t); }
    });
  });
  return out.slice(0, MAX_THREADS_PER_RUN);
}
function forwardInvoicesToPennylane() {
  var sent = getOrCreateLabel_(SENT_LABEL);
  var selfEmail = (Session.getActiveUser().getEmail() || '').toLowerCase();
  var threads = gatherThreads_();
  Logger.log('Conversations a traiter : ' + threads.length);
  var processed = 0;
  for (var i = 0; i < threads.length; i++) {
    try { processThread_(threads[i], selfEmail); threads[i].addLabel(sent); processed++; }
    catch (err) { Logger.log('Erreur sur "' + threads[i].getFirstMessageSubject() + '": ' + err); }
  }
  Logger.log('Envoyees a Pennylane : ' + processed);
}
function processThread_(thread, selfEmail) {
  var messages = thread.getMessages();
  for (var i = 0; i < messages.length; i++) {
    var message = messages[i];
    if (selfEmail && (message.getFrom() || '').toLowerCase().indexOf(selfEmail) !== -1) continue;
    var files = collectInvoiceFiles_(message);
    if (files.length === 0) continue;
    var subject = message.getSubject() || 'Facture';
    GmailApp.sendEmail(PENNYLANE_ADDRESS, '[Facture] ' + subject,
      'Transferee automatiquement depuis ' + message.getFrom() + '\nDate : ' + message.getDate(),
      { attachments: files, name: 'Pennylane Mailer' });
  }
}
function collectInvoiceFiles_(message) {
  var files = [];
  var att = message.getAttachments({ includeInlineImages: false, includeAttachments: true });
  for (var i = 0; i < att.length; i++) {
    var ct = (att[i].getContentType() || '').toLowerCase();
    if (INVOICE_ATTACHMENT_TYPES.indexOf(ct) !== -1) files.push(att[i].copyBlob());
  }
  if (files.length === 0) files.push(bodyToPdf_(message));
  return files;
}
function bodyToPdf_(message) {
  var html = message.getBody() || ('<pre>' + escapeHtml_(message.getPlainBody()) + '</pre>');
  var name = sanitize_(message.getSubject() || 'facture') + '.pdf';
  return Utilities.newBlob(html, 'text/html', 'tmp.html').getAs('application/pdf').setName(name);
}
function getOrCreateLabel_(name) { return GmailApp.getUserLabelByName(name) || GmailApp.createLabel(name); }
function sanitize_(s) { return String(s).replace(/[^a-zA-Z0-9 _-]/g, '').trim().slice(0, 80) || 'facture'; }
function escapeHtml_(s) { return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;'); }
function previewMatches() {
  var a = GmailApp.search(buildAllowlistQuery_(), 0, MAX_THREADS_PER_RUN);
  var h = GmailApp.search(buildHeuristicQuery_(), 0, MAX_THREADS_PER_RUN);
  Logger.log('--- Filet 1 liste blanche : ' + a.length + ' ---');
  for (var i = 0; i < a.length; i++) Logger.log('  [L] ' + a[i].getFirstMessageSubject());
  Logger.log('--- Filet 2 nouveaux emetteurs : ' + h.length + ' ---');
  for (var j = 0; j < h.length; j++) Logger.log('  [H] ' + h[j].getFirstMessageSubject());
  Logger.log('=> Total unique : ' + gatherThreads_().length);
}
function resetSentLabel() {
  var label = GmailApp.getUserLabelByName(SENT_LABEL);
  if (!label) { Logger.log('Aucun libelle a reinitialiser.'); return; }
  var removed = 0, threads;
  do { threads = label.getThreads(0, 100);
    for (var i = 0; i < threads.length; i++) { threads[i].removeLabel(label); removed++; }
  } while (threads.length > 0);
  Logger.log('Libelle retire de ' + removed + ' conversation(s).');
}
function installHourlyTrigger() {
  ScriptApp.getProjectTriggers().forEach(function (t) {
    if (t.getHandlerFunction() === 'forwardInvoicesToPennylane') ScriptApp.deleteTrigger(t);
  });
  ScriptApp.newTrigger('forwardInvoicesToPennylane').timeBased().everyHours(1).create();
  Logger.log('Declencheur horaire installe.');
}
