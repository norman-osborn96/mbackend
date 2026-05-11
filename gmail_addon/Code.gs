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
  var body = message.getPlainBody().substring(0, 3000); // Increased limit for better context
  
  // Call Backend
  var tone = (e.formInput && e.formInput.tone) || (e.parameters && e.parameters.tone) || "professional";
  var analysis = fetchAnalysis(subject, body, sender, tone);
  
  var card = CardService.newCardBuilder();
  card.setHeader(CardService.newCardHeader().setTitle("MailPulse AI Analysis"));
  
  // 1. Priority Section
  var prioritySection = CardService.newCardSection();
  var level = (analysis.level || analysis.priority || "LOW").toUpperCase();
  var actionStatus = analysis.ai_action === "REQUIRES_REPLY" ? "🚨 ACTION REQUIRED" : "ℹ️ FYI ONLY";
  
  prioritySection.addWidget(CardService.newKeyValue()
    .setTopLabel("Priority Level")
    .setContent("<b>" + level + "</b>")
    .setBottomLabel(actionStatus)
    .setIcon(getPriorityIcon(level)));
    
  if (analysis.confidence) {
    prioritySection.addWidget(CardService.newKeyValue()
      .setTopLabel("AI Confidence")
      .setContent((analysis.confidence * 100).toFixed(0) + "%")
      .setIcon(CardService.Icon.STAR));
  }
  
  if (analysis.score !== undefined) {
    prioritySection.addWidget(CardService.newKeyValue()
      .setTopLabel("Impact Score")
      .setContent("<b>" + analysis.score + "</b>")
      .setIcon(CardService.Icon.BOOKMARK));
  }
  
  card.addSection(prioritySection);

  // 2. Sender Section
  var senderSection = CardService.newCardSection().setHeader("Sender Analysis");
  var senderEmail = extractEmailAddress(sender);
  senderSection.addWidget(CardService.newKeyValue()
    .setTopLabel("From")
    .setContent(senderEmail)
    .setMultiline(true));
  
  if (analysis.sender_email && analysis.sender_email.includes("@")) {
    var domain = analysis.sender_email.split("@")[1];
    senderSection.addWidget(CardService.newKeyValue()
      .setTopLabel("Organization Domain")
      .setContent(domain));
  }
  card.addSection(senderSection);

  // 3. Summary Section
  if (analysis.summary) {
    var summarySection = CardService.newCardSection().setHeader("✦ AI Summary");
    summarySection.addWidget(CardService.newTextParagraph().setText(analysis.summary));
    card.addSection(summarySection);
  }
  
  // 4. Reasons Section ("Why this rank")
  if (analysis.reasons && analysis.reasons.length > 0) {
    var reasonsSection = CardService.newCardSection().setHeader("Detailed Priority Reasons");
    var reasonsText = analysis.reasons.map(function(r) { return "• " + r; }).join("\n");
    reasonsSection.addWidget(CardService.newTextParagraph().setText(reasonsText));
    card.addSection(reasonsSection);
  }
  
  // 5. Suggestion Section
  var suggestionSection = CardService.newCardSection().setHeader("AI Response Suggestion");
  
  // Tone Selection Dropdown
  var currentTone = e.parameters.tone || "professional";
  var toneDropdown = CardService.newSelectionInput()
    .setType(CardService.SelectionInputType.DROPDOWN)
    .setTitle("Reply Tone")
    .setFieldName("tone")
    .addItem("Professional", "professional", currentTone === "professional")
    .addItem("Friendly", "friendly", currentTone === "friendly")
    .addItem("Short & Concise", "concise", currentTone === "concise")
    .addItem("Formal", "formal", currentTone === "formal")
    .addItem("Casual", "casual", currentTone === "casual")
    .setOnChangeAction(CardService.newAction().setFunctionName("onToneChange"));
  
  suggestionSection.addWidget(toneDropdown);

  if (analysis.suggestion) {
    suggestionSection.addWidget(CardService.newTextParagraph().setText(analysis.suggestion));
    
    // Create Draft Button
    var draftAction = CardService.newAction().setFunctionName("onGenerateDraft").setParameters({
      reply: analysis.suggestion,
      messageId: messageId
    });
    suggestionSection.addWidget(CardService.newTextButton().setText("📝 Create Draft Reply").setOnClickAction(draftAction));
    
    // Copy Button
    var copyAction = CardService.newAction().setFunctionName("onCopyReply").setParameters({reply: analysis.suggestion});
    suggestionSection.addWidget(CardService.newTextButton().setText("📋 Copy to Clipboard").setOnClickAction(copyAction));
  }
  
  card.addSection(suggestionSection);
  
  return card.build();
}

/**
 * Triggered when the tone dropdown changes.
 */
function onToneChange(e) {
  return CardService.newActionResponseBuilder()
    .setNavigation(CardService.newNavigation().updateCard(buildMainCard(e)))
    .build();
}

/**
 * Creates a draft reply in Gmail.
 */
function onGenerateDraft(e) {
  var replyBody = e.parameters.reply;
  var messageId = e.parameters.messageId;
  
  try {
    var message = GmailApp.getMessageById(messageId);
    message.createDraftReply(replyBody);
    
    return CardService.newActionResponseBuilder()
      .setNotification(CardService.newNotification().setText("Draft reply created! Check your Drafts folder."))
      .build();
  } catch (err) {
    return CardService.newActionResponseBuilder()
      .setNotification(CardService.newNotification().setText("Error creating draft: " + err))
      .build();
  }
}

/**
 * Fetches analysis from the backend.
 */
function fetchAnalysis(subject, body, sender, tone) {
  var url = BACKEND_URL + "/addon/analyze";
  var payload = {
    subject: subject,
    snippet: body,
    sender: sender,
    tone: tone || "professional"
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
      return {level: "ERROR", reasons: ["Could not connect to backend: " + response.getResponseCode()]};
    }
  } catch (err) {
    console.error("Fetch Error: " + err);
    return {level: "OFFLINE", reasons: ["Backend unreachable."]};
  }
}

function getPriorityIcon(level) {
  if (level === "HIGH") return CardService.Icon.CONFIRMATION_NUMBER_ICON;
  if (level === "MEDIUM") return CardService.Icon.DESCRIPTION;
  return CardService.Icon.EMAIL;
}

function onCopyReply(e) {
  var reply = e.parameters.reply;
  return CardService.newActionResponseBuilder()
    .setNotification(CardService.newNotification().setText("Suggestion copied! (Simulated)"))
    .build();
}

function extractEmailAddress(s) {
  if (!s) return "";
  var t = s.trim();
  if (t.indexOf("<") !== -1 && t.indexOf(">") !== -1) {
    return t.slice(t.indexOf("<") + 1, t.indexOf(">")).trim().toLowerCase();
  }
  return t.toLowerCase();
}

/**
 * Contextual trigger that runs when a message is opened.
 */
function onGmailMessageOpen(e) {
  return [buildMainCard(e)];
}
