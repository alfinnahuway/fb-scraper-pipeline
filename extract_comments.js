(function() {
    function parseArticle(el) {
        var name = "";
        var nameEl = el.querySelector("a[role='link'] span, a span, h3 span");
        if (nameEl) name = nameEl.textContent.trim();

        var text = "";
        var textEls = el.querySelectorAll("div[dir='auto'] span, span[dir='auto']");
        var texts = [];
        for (var j = 0; j < textEls.length; j++) {
            var t = textEls[j].textContent.trim();
            if (t && t !== name && t.length > 2) texts.push(t);
        }
        text = texts.join(" ");

        var likes = 0;
        var likeEls = el.querySelectorAll("[aria-label*='reaction' i] span, span[class*='reaction']");
        for (var k = 0; k < likeEls.length; k++) {
            var lt = likeEls[k].textContent.trim();
            var nums = lt.match(/[\d,.]+/);
            if (nums) {
                likes = parseInt(nums[0].replace(/[,\.]/g, ""));
                break;
            }
        }

        var timestamp = "";
        var timeEl = el.querySelector("abbr, time, span[class*='timestamp']");
        if (timeEl) {
            timestamp = timeEl.getAttribute("title") || timeEl.getAttribute("datetime") || timeEl.textContent.trim();
        }

        return {
            name: name,
            text: text,
            likes_count: likes,
            timestamp: timestamp,
            comment_id: el.getAttribute("data-commentid") || el.id || ""
        };
    }

    var allArticles = document.querySelectorAll("div[role='article']");
    var topLevel = [];
    var replyList = [];

    for (var i = 0; i < allArticles.length; i++) {
        var art = allArticles[i];
        var aria = (art.getAttribute('aria-label') || '').toLowerCase();

        var depth = 0;
        var p = art.parentElement;
        while (p) {
            if (p.getAttribute && p.getAttribute('role') === 'article') depth++;
            p = p.parentElement;
        }

        var isReply = (aria.indexOf('reply') === 0) || depth > 0;

        if (isReply) {
            replyList.push({ el: art, depth: depth, aria: aria });
        } else {
            topLevel.push({ el: art, idx: topLevel.length });
        }
    }

    var topLevelResult = [];
    for (var t = 0; t < topLevel.length; t++) {
        var parsed = parseArticle(topLevel[t].el);
        parsed.replies = [];
        topLevelResult.push(parsed);
    }

    for (var r = 0; r < replyList.length; r++) {
        var parsedReply = parseArticle(replyList[r].el);
        if (!parsedReply.text || parsedReply.text.length <= 3) continue;

        var ariaText = replyList[r].aria;
        var matchIdx = -1;

        var parentMatch = ariaText.match(/to\s+(.+?)[']?s\s*comment/);
        if (parentMatch) {
            var parentName = parentMatch[1].trim().toLowerCase();
            for (var ti = topLevelResult.length - 1; ti >= 0; ti--) {
                if (topLevelResult[ti].name && topLevelResult[ti].name.toLowerCase().indexOf(parentName) >= 0) {
                    matchIdx = ti;
                    break;
                }
            }
        }

        if (matchIdx < 0) {
            var replyEl = replyList[r].el;
            var bestIdx = -1;
            for (var ti2 = 0; ti2 < topLevel.length; ti2++) {
                if (topLevel[ti2].el.compareDocumentPosition(replyEl) & Node.DOCUMENT_POSITION_FOLLOWING) {
                    bestIdx = ti2;
                } else {
                    break;
                }
            }
            if (bestIdx >= 0) matchIdx = bestIdx;
        }

        if (matchIdx < 0 && topLevelResult.length > 0) {
            matchIdx = topLevelResult.length - 1;
        }

        if (matchIdx >= 0) {
            topLevelResult[matchIdx].replies.push({
                name: parsedReply.name,
                text: parsedReply.text,
                likes_count: parsedReply.likes_count,
                timestamp: parsedReply.timestamp,
                comment_id: parsedReply.comment_id
            });
        }
    }

    var finalResult = [];
    for (var f = 0; f < topLevelResult.length; f++) {
        if (topLevelResult[f].text && topLevelResult[f].text.length > 3) {
            finalResult.push(topLevelResult[f]);
        }
    }
    return finalResult;
})()
