-- 007 社区讨论标题的机器翻译（《优化方案 v1.0》O-14，D67）
-- 中文帖填 title_en，英文帖填 title_zh；页面按语言取，没有译名时显示原标题。原标题变了译名清空重译（repo.save_intel）。
-- translate_failed：整批译文对不上（条数、空值、过长）的次数，到 3 次不再试，免得一条怪标题反复耗 token。

ALTER TABLE intel ADD COLUMN title_zh TEXT;
ALTER TABLE intel ADD COLUMN title_en TEXT;
ALTER TABLE intel ADD COLUMN translate_failed INTEGER NOT NULL DEFAULT 0;
