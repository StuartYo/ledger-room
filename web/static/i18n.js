"use strict";
// Every string the page shows, in Traditional Chinese (default) and English. t("key", {n: 3}) fills {n}.
const I18N = {
  zh: {
    // top bar, rail, menus
    navQueue: "待決定", navDocs: "全部單據", navExport: "匯出申報資料", navLog: "紀錄", navSettings: "設定",
    sample: "示範資料",
    synth: "示範資料：這一頁上的公司、統一編號、發票和收據都是程式產生的，不是真的公司或真人。",
    statusLocal: "只在這台電腦上跑",
    localTip: "文字辨識和模型都在這台電腦上執行（OpenVINO），程式不會對外連線。",
    modelLoading: "模型：載入中…", modelOn: "模型：{m}（{d}）", modelLater: "第一次檢查時才載入模型",
    strict: "嚴格模式", masked: "個資已遮罩", shown: "個資顯示中（已記錄）",
    maskTip: "讀出的欄位、說明、匯出檔和紀錄裡的人名、電話、身分證號都會遮罩（個資安全維護辦法第 17 條）。單據原圖不遮。",
    unmask: "顯示個資", remask: "重新遮罩", unmaskTip: "這次檢查的個資顯示 10 分鐘。要填理由，理由會寫進操作紀錄。",
    logout: "登出", lang: "English", langName: "語言", privacy: "個資", account: "帳號", roleOf: "{u}・{r}",
    search: "搜尋客戶、發票號碼、檔名",
    offline: "連不到這台電腦，恢復後會自動接上。你按的決定都先存在這個瀏覽器，連回來就補送。",
    inbox: "事務所與期別", clients: "客戶", allClients: "全部客戶",
    docsN: "{n} 張", leftN: "還剩 {n} 張要決定",

    // overview: one title, one line, the period strip, the main button
    ovPending: "{n} 張單據等你決定",
    ovMust: "其中 <b>{m} 張必須決定</b>才能匯出",
    ovLookOnly: "剩下的 {n} 張只是建議看一下，不擋匯出",
    riskHeld: "這些單據上的進項稅額共 <b>{t}</b>",
    riskWrong: "照現在直接申報，會錯誤扣抵 <b>{t}</b> 進項稅額",
    riskNote: "在國稅局查到之前自己更正：補稅加利息，不罰款（稅捐稽徵法第 48-1 條）。",
    emptyTitle: "收件匣是空的",
    emptyInbox: "收件匣裡還沒有單據。把這一期的掃描檔放進 <span class='mono'>data/{set}/clients/客戶代號/年-月/</span> 資料夾，再重新整理這一頁。",
    noRunTitle: "這一期還沒檢查過", noRunSub: "按「檢查這一期」，程式會讀完每一張單據，再照稅法規則全部檢查一遍。",
    newDocsTitle: "收件匣有 {n} 張新單據還沒檢查", newDocsSub: "重新檢查這一期，新單據才會算進去。",
    allDoneTitle: "暫緩的單據都決定了", allDoneSub: "可以匯出這一期的申報資料。",
    exportedTitle: "這一期已經匯出", exportedSub: "{who}・{when}。之後有改決定，再匯出一次就好。",
    doneTitle: "讀完了：{n} 張單據，{p} 張要你決定", doneZero: "讀完了：{n} 張單據，沒有要你決定的",
    errTitle: "這次檢查沒有跑完", errSub: "原因：{e}",
    intTitle: "上次檢查在第 {i} / {n} 張停了", intSub: "可能是電腦重開或程式中斷。已讀完的 {d} 張不用重讀。",
    resume: "從第 {k} 張接著讀", restart: "全部重新檢查",
    runBtn: "檢查這一期", recheck: "重新檢查",
    startReview: "開始決定", exportBtn: "匯出申報資料",
    onlyAdmin: "只有管理員可以讓模型讀單據",
    carried: "上次已經決定、而且問題一樣的 {n} 張，沿用上次的決定（紀錄裡寫明出處）。",
    skipped: "有 {n} 個檔案不是圖片，沒有讀：{f}",
    lgMust: "張必須決定", lgLook: "張建議看一下", lgReady: "張可直接入帳", lgDone: "張已決定",
    metaRead: "每張由 <b>OCR</b> 與<b>視覺模型</b>各讀一次",
    metaLast: "上次檢查：{when}・每張約 {s} 秒", metaLastNoS: "上次檢查：{when}",
    howCache: "用上次的讀取結果", history: "歷次檢查",
    periodLine: "{who}：電子發票 {e} 張（平台下載）＋紙本 {p} 張・{h} 張暫緩・{r} 張可入帳",
    thisPeriod: "這一期", thisPeriodOf: "這一期・{c}", periodErr: "整期核對沒有跑：{e}",

    // progress of a running check
    loadingModel: "正在這台電腦上載入模型…", queued: "排隊中…",
    progTitle: "正在檢查這一期", progMain: "第 {i} / {n} 張", progEta: "大約還要 {m} 分鐘", progEtaS: "不到 1 分鐘",
    progPer: "每張約 {s} 秒", progFail: "{n} 張讀不到，會交給人看", progCached: "沿用已讀過的結果",
    progLeave: "檢查在這台電腦上跑，可以關掉這一頁，回來會自動接上。",

    // the queue
    tabTodo: "待決定", tabDone: "已決定",
    byPriority: "依優先順序", byReason: "依原因分組",
    thClient: "客戶", thNo: "發票號碼", thWhy: "為什麼暫緩", thTotal: "總計", thTax: "稅額", thSug: "建議", thDecision: "決定",
    grpMust: "必須決定", grpLook: "建議看一下・不擋匯出",
    decideAll: "整組決定…", collapse: "收合", expand: "展開",
    noRunList: "還沒有檢查結果。先按「檢查這一期」。", noTodo: "沒有要決定的單據。", noDone: "還沒有決定過的單據。",
    noMatch: "沒有符合「{q}」的單據。",
    must: "必須決定", should: "建議看一下",
    checkPaper: "先對照紙本",
    decidedBy: "{who}・{when}", saving: "儲存中…",

    // right column of the overview
    chkHead: "匯出前",
    chkReadOk: "{n} 張都讀過了", chkReadOkSub: "沒有讀不到的檔案", chkReadSkip: "{n} 個檔案不是圖片，沒有讀",
    chkNone: "這一期還沒檢查", chkNew: "有 {n} 張新單據還沒檢查", chkRun: "檢查中：{i} / {n}", chkReadSub: "檢查完才能匯出",
    chkDecOk: "暫緩的單據都決定了", chkDecNo: "還有 {n} 張必須決定", chkDecSub: "決定完才能匯出",
    chkLogOk: "紀錄完整", chkLogOkSub: "雜湊鏈與簽章都驗過", chkLogBad: "紀錄驗證沒過", chkLogBadSub: "到「紀錄」頁看是哪一筆",
    chkLogNone: "還沒有決定紀錄", chkLogNoneSub: "第一個決定會開始記",
    chkExpOk: "已匯出（{when}）", chkExpOkSub: "{who}。之後有改決定要再匯出", chkExpNo: "還沒匯出", chkExpNoSub: "覆核用 CSV，欄位照進項媒體申報檔",

    // the review screen
    revLeft: "剩 {n} 張", pos: "第 {k} 張，共 {n} 張",
    whyMust: "必須決定・為什麼暫緩", whyLook: "建議看一下・為什麼標出來", also: "另外",
    reasonLaw: "理由與法條", dWhy: "原因", dLaw: "依據", dEvidence: "證據", dCheck: "要你確認",
    decideLbl: "按一個鍵決定",
    d_confirm: "核對無誤", d_nondeductible: "不得扣抵", d_exclude: "剔除", d_defer: "留到下期",
    d_confirm_sub: "照規則入帳", d_nondeductible_sub: "入帳，扣抵代號 3", d_exclude_sub: "本期不申報", d_defer_sub: "下期再扣抵",
    d_confirm_tip: "對過紙本，讀得沒錯：照規則的結果申報", d_nondeductible_tip: "放進申報檔，扣抵代號改 3（不得扣抵）",
    d_exclude_tip: "這一期不申報（重複、作廢、不是這家客戶的）", d_defer_tip: "這一期先不報，之後再扣抵（營業稅法施行細則第 29 條）",
    suggested: "建議", decidedAs: "已決定：{d}", change: "改決定",
    readLbl: "讀到的內容", readAgree: "{n} 欄有 {a} 欄兩次讀取一致", showAll: "顯示全部欄位", showKey: "只框問題欄位",
    readOnce: "只有一邊讀到", provTip: "來源標記：{p}",
    zoomIn: "放大", zoomOut: "縮小", fit: "符合視窗", openOriginal: "開原圖", back: "回到清單",
    stakeTax: "稅額 {t}", stakeSame: "同一張：{d}",
    viewNone: "這張沒有問題。", viewNotRun: "還沒檢查這張。", notRead: "還沒讀。",
    einvTitle: "電子發票・平台下載",
    einvNoPhoto: "這一筆是從財政部平台下載的電子發票資料，不是照片。需要時到平台查明細。", einvFields: "平台下載的資料，不用讀取。",
    sameVoucher: "同一張單據",

    // fields
    f_doc_type: "類型", f_invoice_no: "發票號碼", f_date: "日期", f_invoice_period: "發票期別", f_seller_name: "賣方",
    f_seller_ubn: "賣方統編", f_buyer_name: "買方", f_buyer_ubn: "買方統編", f_items: "品項", f_net: "銷售額",
    f_tax: "稅額", f_total: "總計", f_note: "備註", f_category: "類別（模型判斷）",

    // decisions: toasts, group decisions
    toastOne: "{c}：{d}", toastGroup: "{n} 張已決定為「{d}」", toastUndone: "已復原 {n} 張", undo: "復原", taken: "已收回決定",
    sugPaper: "建議：先對照紙本",
    ev_run_start: "開始檢查", ev_doc_read: "讀取單據", ev_exception: "抓到問題", ev_run_end: "檢查結束", ev_run_done: "檢查完成",
    ev_run_failed: "檢查失敗", ev_review: "決定", ev_review_carried_over: "沿用上次決定", ev_export: "匯出", ev_export_media_csv: "匯出申報 CSV",
    ev_export_summary_csv: "匯出 401 合計", ev_view_result: "查看結果", ev_view_audit_log: "查看稽核紀錄", ev_view_oplog: "查看操作紀錄",
    ev_login: "登入", ev_login_failed: "登入失敗", ev_login_locked: "登入鎖定", ev_logout: "登出", ev_denied: "拒絕存取", ev_unmask: "顯示個資",
    ev_server_start: "程式啟動", ev_encryption_check: "加密檢查", ev_purge_start: "開始清除", ev_purge_file: "清除檔案", ev_purge_end: "清除完成",
    ev_log_tail_broken: "紀錄尾端不完整", ev_cached: "沿用",
    gdTitle: "整組決定：{r}", gdBody: "這一組有 {n} 張還沒決定。每一張都會用你的名字寫進紀錄，按「復原」可以收回。",
    gdPick: "全部決定為", gdGo: "決定這 {n} 張", gdMore: "還有 {n} 張", saveFail: "這個決定沒有存進去：{e}",

    // key bar
    kMove: "上下張", kDecide: "決定", kOpen: "打開", kGroup: "依原因分組", kDecideGroup: "整組決定", kSearch: "搜尋",
    kUndo: "復原", kBack: "回清單", kReason: "理由與法條", kFields: "全部欄位", kChange: "改決定",

    // all vouchers
    docsTitle: "全部單據", docsSub: "{n} 張・{m} 家客戶", st_READY: "可申報", st_HOLD: "暫緩", st_NOT_IN_FILE: "不入申報", noEx: "沒有問題",

    // logs
    logTitle: "紀錄", auditHead: "稽核紀錄", auditSub: "這次檢查的每次讀取、每個問題",
    logAdmin: "稽核紀錄和操作紀錄只有管理員看得到。", logNone: "還沒有檢查。", howVerified: "怎麼驗證的？",
    logExplain: "每讀一張、每抓到一個問題都會記一筆：單據的 SHA-256 指紋、用的模型和硬體。每筆都連著上一筆的雜湊值，並用操作金鑰簽章（金鑰放在資料夾以外）。沒有金鑰的人改、插、刪任何一筆都抓得到；同時拿到硬碟和金鑰的人擋不住。對應記帳業個資安全維護辦法第 19 條（使用紀錄與軌跡資料至少保存五年）。",
    opHead: "操作紀錄", opSub: "誰登入、檢查、查看、匯出、做了什麼決定",
    lt_seq: "#", lt_time: "時間", lt_event: "事件", lt_doc: "單據", lt_detail: "內容", lt_hash: "雜湊", lt_user: "使用者",
    v_broken: "第 {n} 筆之後雜湊鏈斷了", v_mac: "第 {n} 筆之後簽章對不上：有人沒用金鑰改了紀錄",
    v_tail: "紀錄尾端少了幾筆（簽章過的尾端紀錄指到更後面）", v_unsigned: "雜湊鏈驗證通過（{n} 筆）。這次檢查在加入簽章之前，沒有簽章。",
    v_ok: "紀錄完整，沒有被改過（{n} 筆，雜湊鏈和簽章都驗證通過）", v_nokey: "雜湊鏈驗證通過（{n} 筆）；這台電腦找不到操作金鑰，沒有驗簽章",

    // dialogs
    confirmRunTitle: "檢查這一期", confirmRunBody: "要讀 <b>{n}</b> 張單據（{m} 家客戶）。",
    confirmRunEta: "大約 {min} 分鐘（每張約 {s} 秒）。", confirmRunLeave: "檢查在這台電腦上跑：可以關掉這一頁，回來會接上進度。",
    start: "開始檢查", cancel: "取消", close: "關閉",
    confirmExpTitle: "匯出這一期的申報資料",
    expReady: "進申報檔", expNonded: "其中不得扣抵", expExclude: "剔除", expDefer: "留到下期", expNotIn: "本來就不入申報",
    expHold: "還沒決定", expNote: "下載的是覆核用 CSV，欄位照進項媒體申報檔排，記帳士簽核前還是要逐筆看過。",
    expAgain: "這一期 {when}已由 {who} 匯出過，再匯出會產生新的檔案。", expGo: "下載 CSV", expSummary: "另外下載：401 免用發票收據合計",
    expLocked: "還有 {n} 張必須決定。決定完才能匯出。", expNoRun: "這一期還沒檢查，沒有東西可以匯出。",
    loginTitle: "登入 Ledger Room", loginSub: "請向事務所管理員索取帳號。", user: "帳號", password: "密碼", signIn: "登入",
    loginFail: "登入失敗",
    unmaskAsk: "為什麼需要看這次檢查的個資？（理由會寫進操作紀錄）", notAllowed: "沒有權限",
    foot: "法條連結到全國法規資料庫（law.moj.gov.tw）和財政部財政資訊中心。",
    justNow: "剛剛", minAgo: "{n} 分鐘前", hrAgo: "{n} 小時前", dayAgo: "{n} 天前",
    titlePending: "（{n}）待決定・Ledger Room",
  },
  en: {
    navQueue: "To decide", navDocs: "All vouchers", navExport: "Export filing data", navLog: "Logs", navSettings: "Settings",
    sample: "Sample data",
    synth: "Sample data: every company, tax ID, invoice and receipt on this page was generated by code. No real businesses or people.",
    statusLocal: "On this computer",
    localTip: "OCR and the model run on this machine through OpenVINO. The app makes no outbound requests.",
    modelLoading: "Model: loading…", modelOn: "Model: {m} ({d})", modelLater: "The model loads on the first check",
    strict: "strict", masked: "Personal data masked", shown: "Personal data shown (logged)",
    maskTip: "Personal names, phone numbers and ID numbers in the read fields, messages, exports and logs are masked (safeguard regulation Art. 17). Voucher images are not masked.",
    unmask: "Show personal data", remask: "Mask again", unmaskTip: "Shows personal data for this check for 10 minutes. You give a reason; it goes to the operation log.",
    logout: "Log out", lang: "中文", langName: "Language", privacy: "Personal data", account: "Account", roleOf: "{u} · {r}",
    search: "Search clients, invoice numbers, files",
    offline: "Can't reach this computer. It reconnects by itself; decisions you make are kept in this browser and sent when it's back.",
    inbox: "Firm and period", clients: "Clients", allClients: "All clients",
    docsN: "{n} vouchers", leftN: "{n} left to decide",

    ovPending: "{n} vouchers are waiting for you",
    ovMust: "<b>{m} must be decided</b> before export",
    ovLookOnly: "the other {n} are worth a look and do not block the export",
    riskHeld: "<b>{t}</b> of input tax is on these vouchers",
    riskWrong: "filed as they are, <b>{t}</b> of input tax would be claimed wrongly",
    riskNote: "Corrected before the tax office asks: tax plus interest, no fine (Tax Collection Act Art. 48-1).",
    emptyTitle: "The inbox is empty",
    emptyInbox: "No vouchers in this inbox yet. Put this period's scans in <span class='mono'>data/{set}/clients/CLIENT/YYYY-MM/</span>, then reload this page.",
    noRunTitle: "This period has not been checked", noRunSub: "Check this period: the app reads every voucher, then checks them all against the tax rules.",
    newDocsTitle: "{n} new vouchers are not checked yet", newDocsSub: "Check this period again to include them.",
    allDoneTitle: "Every held voucher is decided", allDoneSub: "You can export this period's filing data.",
    exportedTitle: "This period has been exported", exportedSub: "{who} · {when}. If you change a decision, export again.",
    doneTitle: "Done: {n} vouchers read, {p} need your decision", doneZero: "Done: {n} vouchers read, nothing needs a decision",
    errTitle: "This check did not finish", errSub: "Reason: {e}",
    intTitle: "The last check stopped at voucher {i} of {n}", intSub: "The computer restarted or the app stopped. The {d} vouchers already read are not read again.",
    resume: "Continue from voucher {k}", restart: "Check everything again",
    runBtn: "Check this period", recheck: "Check again",
    startReview: "Start deciding", exportBtn: "Export filing data",
    onlyAdmin: "Only an admin can have the model read vouchers",
    carried: "{n} vouchers decided last time with the same issues keep that decision (the log says where it came from).",
    skipped: "{n} files are not images and were not read: {f}",
    lgMust: "must decide", lgLook: "worth a look", lgReady: "ready to file", lgDone: "decided",
    metaRead: "Each voucher read twice: <b>OCR</b> and the <b>vision model</b>",
    metaLast: "Last check {when} · about {s} s each", metaLastNoS: "Last check {when}",
    howCache: "from the last readings", history: "Earlier checks",
    periodLine: "{who}: {e} e-invoices (platform download) + {p} paper · {h} held · {r} ready to book",
    thisPeriod: "This period", thisPeriodOf: "This period, {c}", periodErr: "Period check not run: {e}",

    loadingModel: "Loading the model on this computer…", queued: "Queued…",
    progTitle: "Checking this period", progMain: "Voucher {i} of {n}", progEta: "about {m} min left", progEtaS: "under a minute left",
    progPer: "about {s} s each", progFail: "{n} could not be read; a person will look at them", progCached: "reused an earlier reading",
    progLeave: "The check runs on this computer: you can close this page and come back.",

    tabTodo: "To decide", tabDone: "Decided",
    byPriority: "By priority", byReason: "Group by reason",
    thClient: "Client", thNo: "Invoice no.", thWhy: "Why it was held", thTotal: "Total", thTax: "Tax", thSug: "Suggested", thDecision: "Decision",
    grpMust: "Must decide", grpLook: "Worth a look · does not block the export",
    decideAll: "Decide all…", collapse: "Collapse", expand: "Expand",
    noRunList: "No check yet. Check this period first.", noTodo: "Nothing to decide.", noDone: "Nothing decided yet.",
    noMatch: "No vouchers match “{q}”.",
    must: "Must decide", should: "Worth a look",
    checkPaper: "Check the paper",
    decidedBy: "{who} · {when}", saving: "Saving…",

    chkHead: "Before you export",
    chkReadOk: "All {n} vouchers read", chkReadOkSub: "No unreadable files", chkReadSkip: "{n} files are not images and were not read",
    chkNone: "This period is not checked yet", chkNew: "{n} new vouchers not checked", chkRun: "Checking: {i} of {n}", chkReadSub: "Export unlocks after the check",
    chkDecOk: "Every held voucher decided", chkDecNo: "{n} held vouchers not decided", chkDecSub: "Export unlocks when all are decided",
    chkLogOk: "Log intact", chkLogOkSub: "Hash chain and signature verified", chkLogBad: "Log check failed", chkLogBadSub: "Open Logs to see which entry",
    chkLogNone: "No decisions logged yet", chkLogNoneSub: "The first decision starts the log",
    chkExpOk: "Exported {when}", chkExpOkSub: "{who}. Export again after any change", chkExpNo: "Not exported yet", chkExpNoSub: "Review CSV in the VAT media-file layout",

    revLeft: "{n} left", pos: "{k} of {n}",
    whyMust: "Must decide · why it was held", whyLook: "Worth a look · why it was flagged", also: "Also",
    reasonLaw: "Reason and law", dWhy: "Why", dLaw: "The rule", dEvidence: "Evidence", dCheck: "What to check",
    decideLbl: "Decide with one key",
    d_confirm: "Checked OK", d_nondeductible: "Not deductible", d_exclude: "Leave out", d_defer: "Next period",
    d_confirm_sub: "File it as the rules say", d_nondeductible_sub: "Filed with code 3", d_exclude_sub: "Not filed this period", d_defer_sub: "Claim it next period",
    d_confirm_tip: "Checked against the paper, read correctly: file it as the rules say", d_nondeductible_tip: "Into the filing with deduction code 3 (not deductible)",
    d_exclude_tip: "Not filed this period (a duplicate, voided, not this client's)", d_defer_tip: "Not this period; claim it later (VAT Enforcement Rules Art. 29)",
    suggested: "Suggested", decidedAs: "Decided: {d}", change: "Change decision",
    readLbl: "What was read", readAgree: "{a} of {n} fields read the same twice", showAll: "Show all fields", showKey: "Only the issue",
    readOnce: "Read once only", provTip: "source: {p}",
    zoomIn: "Zoom in", zoomOut: "Zoom out", fit: "Fit", openOriginal: "Open original", back: "Back to the list",
    stakeTax: "tax {t}", stakeSame: "same as {d}",
    viewNone: "No issues on this voucher.", viewNotRun: "Not checked yet.", notRead: "Not read yet.",
    einvTitle: "E-invoice · platform download",
    einvNoPhoto: "This line comes from the Ministry of Finance e-invoice download, not from a photo. Look it up on the platform if needed.", einvFields: "Platform data: nothing to read.",
    sameVoucher: "Same voucher",

    f_doc_type: "Type", f_invoice_no: "Invoice no.", f_date: "Date", f_invoice_period: "Period header", f_seller_name: "Seller",
    f_seller_ubn: "Seller tax ID", f_buyer_name: "Buyer", f_buyer_ubn: "Buyer tax ID", f_items: "Items", f_net: "Net",
    f_tax: "Tax", f_total: "Total", f_note: "Note", f_category: "Category (model)",

    toastOne: "{c}: {d}", toastGroup: "{n} vouchers marked {d}", toastUndone: "Undone: {n} restored", undo: "Undo", taken: "decision taken back",
    sugPaper: "Suggested: check the paper",
    ev_run_start: "check started", ev_doc_read: "voucher read", ev_exception: "issue found", ev_run_end: "check ended", ev_run_done: "check done",
    ev_run_failed: "check failed", ev_review: "decision", ev_review_carried_over: "decision carried over", ev_export: "export", ev_export_media_csv: "filing CSV exported",
    ev_export_summary_csv: "401 total exported", ev_view_result: "result viewed", ev_view_audit_log: "audit log viewed", ev_view_oplog: "operation log viewed",
    ev_login: "signed in", ev_login_failed: "sign-in failed", ev_login_locked: "sign-in locked", ev_logout: "signed out", ev_denied: "access denied", ev_unmask: "personal data shown",
    ev_server_start: "app started", ev_encryption_check: "encryption check", ev_purge_start: "purge started", ev_purge_file: "file purged", ev_purge_end: "purge done",
    ev_log_tail_broken: "log tail incomplete", ev_cached: "cached",
    gdTitle: "Decide all: {r}", gdBody: "{n} vouchers in this group are not decided. Each one goes into the log under your name; Undo takes them back.",
    gdPick: "Decision for all of them", gdGo: "Decide {n} vouchers", gdMore: "and {n} more", saveFail: "This decision was not saved: {e}",

    kMove: "move", kDecide: "decide", kOpen: "open", kGroup: "group by reason", kDecideGroup: "decide group", kSearch: "search",
    kUndo: "undo", kBack: "back", kReason: "reason and law", kFields: "all fields", kChange: "change",

    docsTitle: "All vouchers", docsSub: "{n} vouchers · {m} clients", st_READY: "Ready", st_HOLD: "Held", st_NOT_IN_FILE: "Not a filing line", noEx: "no issues",

    logTitle: "Logs", auditHead: "Audit log", auditSub: "every read and every issue in this check",
    logAdmin: "The audit log and the operation log are visible to admins only.", logNone: "No check yet.", howVerified: "How is this verified?",
    logExplain: "Every read and every issue is logged with the voucher's SHA-256, the model and the device. Each entry carries the hash of the previous one and an HMAC under the operator key, kept outside the data folder. That detects edits, insertions and deletions by anyone without the key; it cannot stop someone who holds both the disk and the key. Supports Art. 19 of the bookkeepers' personal-data safeguard regulation (usage records kept at least five years).",
    opHead: "Operation log", opSub: "who signed in, checked, viewed, exported and decided",
    lt_seq: "#", lt_time: "Time", lt_event: "Event", lt_doc: "Voucher", lt_detail: "Detail", lt_hash: "Hash", lt_user: "User",
    v_broken: "Hash chain broken after entry {n}", v_mac: "HMAC mismatch after entry {n}: an entry was changed without the operator key",
    v_tail: "Entries are missing from the end of the log (the signed head record points past the last entry)", v_unsigned: "Hash chain verified ({n} entries). This check was logged before HMAC signing was added.",
    v_ok: "Log intact: {n} entries, hash chain and HMAC verified", v_nokey: "Hash chain verified ({n} entries); operator key not found on this machine, HMAC not checked",

    confirmRunTitle: "Check this period", confirmRunBody: "Reads <b>{n}</b> vouchers ({m} clients).",
    confirmRunEta: "About {min} min (about {s} s per voucher).", confirmRunLeave: "The check runs on this computer: you can close this page and the progress will be here when you come back.",
    start: "Start the check", cancel: "Cancel", close: "Close",
    confirmExpTitle: "Export this period's filing data",
    expReady: "Go into the filing", expNonded: "of which not deductible", expExclude: "Left out", expDefer: "Next period", expNotIn: "Not filing lines anyway",
    expHold: "Not decided yet", expNote: "This is the review CSV laid out like the input records of the VAT media file. The bookkeeper still checks every line before signing off.",
    expAgain: "{who} already exported this period {when}. Exporting again makes a new file.", expGo: "Download CSV", expSummary: "Also: 401 small-business receipts total",
    expLocked: "{n} vouchers must be decided first. Export unlocks when they are.", expNoRun: "This period has not been checked; there is nothing to export.",
    loginTitle: "Sign in to Ledger Room", loginSub: "Ask the firm's admin for an account.", user: "User", password: "Password", signIn: "Sign in",
    loginFail: "Sign-in failed",
    unmaskAsk: "Why do you need to see personal data for this check? (goes to the operation log)", notAllowed: "Not allowed",
    foot: "Rule citations link to the Laws & Regulations Database of the R.O.C. (law.moj.gov.tw) and the MOF Fiscal Information Agency.",
    justNow: "just now", minAgo: "{n} min ago", hrAgo: "{n} h ago", dayAgo: "{n} days ago",
    titlePending: "({n}) to decide · Ledger Room",
  },
};

// Each rule: a one-line verdict (v) for the list and the top of the review panel, and what to check (check).
// The full explanation stays: Chinese has its own title and why (zh.title, zh.why); English uses the rule engine's
// own title and message, which carry the voucher's specifics. Both are shown under "Reason and law".
const RULES_TEXT = {
  UBN_CHECKSUM: {
    zh: { v: "統一編號檢查碼不符", title: "統一編號檢查碼不對", why: "統編的檢查碼算不過：可能讀錯，也可能發票本身有誤。請對照紙本。", check: "對照紙本上的統編。讀錯了就照紙本為準；發票本身印錯，請賣方重開。" },
    en: { v: "Tax ID fails its check digit", check: "Compare the tax ID with the paper. If the invoice itself is wrong, ask the seller for a new one." } },
  DUP_CROSS_MONTH: {
    zh: { v: "同一張單據交了兩次", title: "重複單據：同一家客戶交過同一張", why: "同一張發票在這家客戶出現兩次。重複扣抵會被認定虛報進項。", check: "只留一張；另一張選「剔除」。" },
    en: { v: "Same voucher handed in twice", check: "Keep one copy and leave the other out." } },
  DUP_CROSS_CLIENT: {
    zh: { v: "兩家客戶有同一張", title: "重複單據：同一張出現在兩家客戶", why: "同一張單據出現在兩家客戶的資料夾，只能算在其中一家。", check: "問清楚這張屬於哪一家；另一家選「剔除」。" },
    en: { v: "Same voucher under two clients", check: "Ask which client it belongs to; leave it out of the other." } },
  S19_NONDEDUCTIBLE: {
    zh: { v: "進項不得扣抵・第 19 條", title: "依營業稅法第 19 條不得扣抵", why: "品項看起來是交際、員工福利或非營業用，進項稅額不能扣抵。", check: "如果是營業用或要轉賣的貨，選「核對無誤」。" },
    en: { v: "Input tax not deductible · §19", check: "If it was bought for resale or as a business cost, choose Checked OK." } },
  OVER_LIMIT: {
    zh: { v: "超過客戶報支上限", title: "超過客戶自訂的報支上限", why: "金額超過這家客戶設定的單筆上限，請確認有沒有核准。", check: "確認客戶有沒有核准這筆。" },
    en: { v: "Over the client's expense limit", check: "Check that the client approved it." } },
  PERIOD_MISMATCH: {
    zh: { v: "日期不在本期", title: "單據日期不在這一期", why: "日期不在本期範圍。前期的單據可以延到這期扣抵（施行細則第 29 條），本期之後的要留到下期。", check: "前期的可以本期扣抵；本期之後的選「留到下期」。" },
    en: { v: "Date outside this filing period", check: "Earlier dates can still be claimed now; later ones go to the next period." } },
  READ_REVIEW: {
    zh: { v: "有欄位要對照紙本", title: "請人看一下：有欄位在紙上確認不了", why: "要填進申報檔的欄位只有一個來源，或整張讀不到。請對照紙本。", check: "看圖上標出來的欄位，和紙本對一次。" },
    en: { v: "A field needs the paper", check: "Read the marked fields off the paper." } },
  AMOUNT_MISMATCH: {
    zh: { v: "金額加總對不起來", title: "金額對不起來", why: "未稅金額加稅額不等於總額，或稅額不是未稅金額的 5%。", check: "對照紙本上的銷售額、稅額和總計。" },
    en: { v: "Amounts don't add up", check: "Compare net, tax and total with the paper." } },
  BUYER_NOT_CLIENT: {
    zh: { v: "買方不是這家客戶", title: "買方統編不是這家客戶", why: "沒有買方統編，或統編不是這家客戶：不能扣抵進項，只能當費用憑證。", check: "沒有客戶統編就只能列費用，選「不得扣抵」。" },
    en: { v: "Buyer is not this client", check: "Without the client's tax ID it is an expense voucher only." } },
  BUYER_PRESUMED: {
    zh: { v: "買方統編讀不到", title: "買方統編讀不到，名稱像是這家客戶", why: "買方統編讀不出來，但買方名稱像這家客戶。請對照紙本。", check: "對照紙本上的買方統編。" },
    en: { v: "Buyer tax ID unreadable", check: "Read the buyer's tax ID off the paper." } },
  NOT_A_VOUCHER: {
    zh: { v: "不是正式憑證", title: "不是正式憑證", why: "這是交易明細或付款單，沒有發票號碼，不能單獨當憑證。", check: "找出這筆的發票或收據。" },
    en: { v: "Not a valid voucher", check: "Find the invoice or receipt for this payment." } },
  SMALL_RECEIPT_CAP: {
    zh: { v: "免用發票收據超過 3%", title: "免用發票收據超過費用的 3%", why: "小規模營業人的收據合計超過製造費用加營業費用的 3%。", check: "看 401 收據合計，超過的部分不能列。" },
    en: { v: "Small receipts over the 3% cap", check: "See the 401 receipts total; the excess cannot be claimed." } },
  UBN_NOT_REGISTERED: {
    zh: { v: "賣方統編查無登記", title: "統編檢查碼對，但查不到營業登記", why: "統編格式正確，但不在營業登記資料裡。請確認賣方。", check: "確認賣方是不是還在營業。" },
    en: { v: "Seller tax ID not registered", check: "Check that the seller is a registered business." } },
  RECEIPT_TAX_UNVERIFIED: {
    zh: { v: "收據稅額可能不能扣", title: "收據上有稅額，但不確定能扣抵", why: "只有財政部核定的憑證種類才能扣抵進項稅額。請確認這張是哪一種。", check: "確認這張是哪一種憑證。" },
    en: { v: "Tax on a receipt may not count", check: "Check which kind of voucher this is." } },
  TRANSPORT_TICKET: {
    zh: { v: "交通票・稅額按 5/105", title: "交通票：進項稅額以總額 5/105 計", why: "請確認是員工出差用。", check: "確認是員工出差用。" },
    en: { v: "Transport ticket · tax is 5/105", check: "Check it was for a business trip." } },
  CUSTOMS_KEY_BY_HAND: {
    zh: { v: "海關繳納證要手動輸入", title: "海關代徵營業稅繳納證：請人工輸入", why: "繳納證號碼和稅額要手動輸入。", check: "照紙本輸入繳納證號碼和稅額。" },
    en: { v: "Customs certificate · key in by hand", check: "Type the certificate number and tax from the paper." } },
  FIXED_ASSET_HINT: {
    zh: { v: "可能是固定資產", title: "可能是固定資產", why: "金額 8 萬以上或品項像設備：扣抵代號可能要改成 2（或 4）。", check: "是設備的話，扣抵代號改 2（或 4）。" },
    en: { v: "Possibly a fixed asset", check: "If it is equipment, the deduction code may be 2 (or 4)." } },
  VOID_ON_PLATFORM: {
    zh: { v: "平台顯示已作廢", title: "電子發票平台顯示已作廢", why: "這張發票已作廢，不能扣抵。", check: "作廢的發票選「剔除」。" },
    en: { v: "Voided on the e-invoice platform", check: "A voided invoice is left out." } },
  UTILITY_BILL_PLATFORM: {
    zh: { v: "水電帳單・以平台為準", title: "水電帳單：進項以平台電子發票為準", why: "進項稅額以平台下載的電子發票為準，這張帳單留著當付款證明。", check: "這張帳單不用申報，留著當付款證明。" },
    en: { v: "Utility bill · platform counts", check: "File the platform e-invoice; keep the bill as proof of payment." } },
  IN_PLATFORM: {
    zh: { v: "已在平台下載資料裡", title: "已在電子發票平台的下載資料裡", why: "這張已經在平台下載的資料裡，不重複輸入。", check: "不用再輸入一次。" },
    en: { v: "Already in the platform download", check: "Do not key it in again." } },
  DUP_PRIOR_FILING: {
    zh: { v: "前期已經申報過", title: "前期已經申報過", why: "這張在之前的申報已經扣抵過。", check: "前期已扣抵，這期選「剔除」。" },
    en: { v: "Already filed in an earlier period", check: "It was claimed before; leave it out now." } },
  PLATFORM_AMOUNT_DIFF: {
    zh: { v: "照片和平台金額不同", title: "照片和平台資料對不上", why: "拍到的發票和平台上的紀錄金額不一樣。請對照紙本。", check: "對照紙本，看哪邊的金額才對。" },
    en: { v: "Photo and platform amounts differ", check: "Check the paper to see which amount is right." } },
  PLATFORM_RECORD_CONFLICT: {
    zh: { v: "平台資料金額矛盾", title: "平台資料裡同一張發票金額不同", why: "同一張發票在平台下載資料裡出現兩次，金額不同。", check: "到平台查這張發票的正確金額。" },
    en: { v: "Platform lists it twice, differently", check: "Look the invoice up on the platform." } },
};

// What a reviewer would usually do with a voucher held for this rule (a suggestion; the person decides)
const SUGGEST = { DUP_CROSS_MONTH: "exclude", DUP_CROSS_CLIENT: "exclude", DUP_PRIOR_FILING: "exclude", VOID_ON_PLATFORM: "exclude",
  PLATFORM_RECORD_CONFLICT: null, S19_NONDEDUCTIBLE: "nondeductible" };

// Law names for citation links in Chinese (pcode on law.moj.gov.tw)
const LAW_ZH = { G0340080: "營業稅法", G0340081: "營業稅法施行細則", G0340051: "營利事業所得稅查核準則", G0340003: "所得稅法" };

// Voucher kinds as the reader names them
const DOC_TYPE = {
  zh: { triplicate_invoice: "三聯式發票", e_invoice: "電子發票證明聯", two_part_register: "二聯式收銀機發票", receipt: "收據",
        transport_ticket: "交通票", customs_payment: "海關代徵營業稅繳納證" },
  en: { triplicate_invoice: "Triplicate invoice", e_invoice: "E-invoice printout", two_part_register: "Two-part register invoice",
        receipt: "Receipt", transport_ticket: "Transport ticket", customs_payment: "Customs VAT certificate" },
};
