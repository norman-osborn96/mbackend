/**
 * MailPulse Gmail Add-on
 * Connects to your backend to provide AI summaries and priority levels.
 */

var BACKEND_URL = "https://your-backend-url.com"; // UPDATE THIS
var API_KEY = "mailpulse-addon-secret-key";     // UPDATE THIS IF CHANGED

/**
 * Builds the main card for a message.
 */
function buildMainCard(e) {
  var messageId = e.gmail.messageId;
  var accessToken = e.gmail.accessToken;
  GmailApp.setCurrentMessageAccessToken(accessToken);
  
  var message = GmailApp.getMessageById(messageId);
  var subject = message.getSubject();
  var sender = message.getFrom();
  var body = message.getPlainBody().substring(0, 1500); // Limit body length
  
  // Call Backend
  var analysis = fetchAnalysis(subject, body, sender);
  
  var card = CardService.newCardBuilder();
  card.setHeader(CardService.newCardHeader().setTitle("MailPulse AI Analysis"));
  
  var section = CardService.newCardSection();
  
  // Priority Widget
  var priorityColor = getPriorityColor(analysis.priority);
  section.addWidget(CardService.newKeyValue()
    .setTopLabel("Priority")
    .setContent("<b>" + analysis.priority + "</b>")
    .setBottomLabel(analysis.reason)
    .setIcon(getPriorityIcon(analysis.priority)));
    
  // Summary Widget
  if (analysis.summary) {
    section.addWidget(CardService.newTextParagraph().setText("<b>Summary:</b>\n" + analysis.summary));
  }
  
  // Suggestion Widget
  if (analysis.suggestion) {
    section.addWidget(CardService.newTextParagraph().setText("<b>Suggested Reply:</b>\n" + analysis.suggestion));
    
    // Copy to Clipboard / Action
    var action = CardService.newAction().setFunctionName("onCopyReply").setParameters({reply: analysis.suggestion});
    section.addWidget(CardService.newTextButton().setText("Use Suggestion").setOnClickAction(action));
  }
  
  card.addSection(section);
  
  return card.build();
}

/**
 * Fetches analysis from the backend.
 */
function fetchAnalysis(subject, body, sender) {
  var url = BACKEND_URL + "/addon/analyze";
  var payload = {
    subject: subject,
    snippet: body,
    sender: sender
  };
  
  var options = {
    method: "post",
    contentType: "application/json",
    headers: {
      "X-API-KEY": API_KEY
    },
    payload: JSON.stringify(payload),
    muteHttpExceptions: true
  };
  
  try {
    var response = UrlFetchApp.fetch(url, options);
    if (response.getResponseCode() == 200) {
      return JSON.parse(response.getContentText());
    } else {
      console.error("Backend Error: " + response.getContentText());
      return {priority: "ERROR", reason: "Could not connect to backend."};
    }
  } catch (err) {
    console.error("Fetch Error: " + err);
    return {priority: "OFFLINE", reason: "Backend unreachable."};
  }
}

function getPriorityColor(level) {
  if (level === "HIGH") return "#ea4335"; // Red
  if (level === "MEDIUM") return "#fbbc04"; // Yellow
  return "#34a853"; // Green
}

function getPriorityIcon(level) {
  if (level === "HIGH") return CardService.Icon.CONFIRMATION_NUMBER_ICON;
  if (level === "MEDIUM") return CardService.Icon.DESCRIPTION;
  return CardService.Icon.EMAIL;
}

function onCopyReply(e) {
  var reply = e.parameters.reply;
  // In a real add-on, we might insert this into the draft, 
  // but for now we'll just show a notification.
  return CardService.newActionResponseBuilder()
    .setNotification(CardService.newNotification().setText("Suggestion ready! Copy from the card above."))
    .build();
}

/**
 * Contextual trigger that runs when a message is opened.
 */
function onGmailMessageOpen(e) {
  return [buildMainCard(e)];
}
