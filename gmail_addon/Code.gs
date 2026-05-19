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
  var body = message.getPlainBody().substring(0, 3000);
  
  // Get tone from form or parameters (safe access)
  var tone = "professional";
  if (e.formInput && e.formInput.tone) {
    tone = e.formInput.tone;
  } else if (e.parameters && e.parameters.tone) {
    tone = e.parameters.tone;
  }
  
  // Call Backend for analysis
  var analysis = fetchAnalysis(subject, body, sender, tone);
  
  var card = CardService.newCardBuilder();
  card.setHeader(CardService.newCardHeader().setTitle("MailPulse AI Analysis"));
  
  // ── 1. Priority Section ──
  var prioritySection = CardService.newCardSection();
  var level = (analysis.level || analysis.priority || "LOW").toUpperCase();
  var actionStatus = analysis.ai_action === "REQUIRES_REPLY" ? "🚨 ACTION REQUIRED" : "ℹ️ FYI ONLY";
  
  prioritySection.addWidget(CardService.newKeyValue()
    .setTopLabel("Priority Level")
    .setContent("<b>" + level + "</b>")
    .setBottomLabel(actionStatus)
    .setIcon(getPriorityIcon(level)));
  
  card.addSection(prioritySection);

  // ── 2. AI Summary Section ──
  var summarySection = CardService.newCardSection().setHeader("✦ AI Summary");
  if (analysis.summary) {
    summarySection.addWidget(CardService.newTextParagraph().setText(analysis.summary));
  } else {
    summarySection.addWidget(CardService.newTextParagraph().setText("<i>Summary not available for this email.</i>"));
  }
  card.addSection(summarySection);
  
  // ── 3. Reasons Section ──
  if (analysis.reasons && analysis.reasons.length > 0) {
    var reasonsSection = CardService.newCardSection().setHeader("Priority Reasons");
    var reasonsText = analysis.reasons.map(function(r) { return "• " + r; }).join("\n");
    reasonsSection.addWidget(CardService.newTextParagraph().setText(reasonsText));
    card.addSection(reasonsSection);
  }
  
  // ── 4. AI Reply Section ──
  var replySection = CardService.newCardSection().setHeader("✍️ AI Reply");
  
  // Tone dropdown
  var toneDropdown = CardService.newSelectionInput()
    .setType(CardService.SelectionInputType.DROPDOWN)
    .setTitle("Reply Tone")
    .setFieldName("tone")
    .addItem("Professional", "professional", tone === "professional")
    .addItem("Friendly", "friendly", tone === "friendly")
    .addItem("Short & Concise", "concise", tone === "concise")
    .addItem("Formal", "formal", tone === "formal")
    .addItem("Casual", "casual", tone === "casual");
  replySection.addWidget(toneDropdown);
  
  // Generate button (always visible)
  var genAction = CardService.newAction()
    .setFunctionName("onGenerateReply")
    .setParameters({
      subject: subject,
      body: body.substring(0, 1500),
      sender: sender,
      messageId: messageId
    });
  replySection.addWidget(CardService.newTextButton()
    .setText("✨ Generate AI Reply")
    .setOnClickAction(genAction)
    .setTextButtonStyle(CardService.TextButtonStyle.FILLED));
  
  // Show the suggestion if backend already returned one
  if (analysis.suggestion) {
    replySection.addWidget(CardService.newTextParagraph().setText(analysis.suggestion));
    
    var draftAction = CardService.newAction()
      .setFunctionName("onGenerateDraft")
      .setParameters({ reply: analysis.suggestion, messageId: messageId });
    replySection.addWidget(CardService.newTextButton()
      .setText("📝 Create Draft Reply")
      .setOnClickAction(draftAction));
  }
  
  card.addSection(replySection);
  
  return card.build();
}

/**
 * Called when user clicks "Generate AI Reply".
 * Makes a separate backend call specifically for reply generation.
 */
function onGenerateReply(e) {
  var subject = e.parameters.subject;
  var body = e.parameters.body;
  var sender = e.parameters.sender;
  var messageId = e.parameters.messageId;
  var tone = (e.formInput && e.formInput.tone) || "professional";
  
  var url = BACKEND_URL + "/addon/generate-reply";
  var payload = {
    subject: subject,
    snippet: body,
    sender: sender,
    tone: tone
  };
  
  var options = {
    method: "post",
    contentType: "application/json",
    headers: { "X-API-KEY": API_KEY },
    payload: JSON.stringify(payload),
    muteHttpExceptions: true
  };
  
  try {
    var response = UrlFetchApp.fetch(url, options);
    var data = JSON.parse(response.getContentText());
    
    if (response.getResponseCode() == 200 && data.reply) {
      // Build a new card showing the reply
      var card = CardService.newCardBuilder();
      card.setHeader(CardService.newCardHeader().setTitle("AI Generated Reply"));
      
      var replySection = CardService.newCardSection();
      replySection.addWidget(CardService.newTextParagraph().setText("<b>Tone:</b> " + tone));
      replySection.addWidget(CardService.newTextParagraph().setText(data.reply));
      
      var draftAction = CardService.newAction()
        .setFunctionName("onGenerateDraft")
        .setParameters({ reply: data.reply, messageId: messageId });
      replySection.addWidget(CardService.newTextButton()
        .setText("📝 Create Draft Reply")
        .setOnClickAction(draftAction)
        .setTextButtonStyle(CardService.TextButtonStyle.FILLED));
      
      card.addSection(replySection);
      
      // Back button
      var backSection = CardService.newCardSection();
      backSection.addWidget(CardService.newTextButton()
        .setText("← Back to Analysis")
        .setOnClickAction(CardService.newAction().setFunctionName("onBackToMain")));
      card.addSection(backSection);
      
      return CardService.newActionResponseBuilder()
        .setNavigation(CardService.newNavigation().pushCard(card.build()))
        .build();
    } else {
      var errMsg = data.detail || data.error || "Unknown error";
      return CardService.newActionResponseBuilder()
        .setNotification(CardService.newNotification().setText("Failed to generate reply: " + errMsg))
        .build();
    }
  } catch (err) {
    return CardService.newActionResponseBuilder()
      .setNotification(CardService.newNotification().setText("Error: " + err))
      .build();
  }
}

/**
 * Navigate back to the main analysis card.
 */
function onBackToMain(e) {
  return CardService.newActionResponseBuilder()
    .setNavigation(CardService.newNavigation().popCard())
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
      .setNotification(CardService.newNotification().setText("✅ Draft reply created! Check your Drafts."))
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
    headers: { "X-API-KEY": API_KEY },
    payload: JSON.stringify(payload),
    muteHttpExceptions: true
  };
  
  try {
    var response = UrlFetchApp.fetch(url, options);
    if (response.getResponseCode() == 200) {
      return JSON.parse(response.getContentText());
    } else {
      console.error("Backend Error: " + response.getContentText());
      return {level: "ERROR", reasons: ["Backend error: " + response.getResponseCode()]};
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
