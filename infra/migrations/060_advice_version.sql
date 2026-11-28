-- 060: which advice prompt wrote a risk's advice (docs/contracts/risk-engine.md §8).
--
-- engine.ADVICE_VERSION names the prompt and the checks the advice was written under. The
-- worker stores it with every advice text it writes, Groq's or the template's. Each worker
-- run re-words open risks whose Groq advice has another version, or none (written before this
-- column), highest score first, within ADVICE_PER_RUN; the old text stays until the new text
-- replaces it. Nullable: no existing row is updated here. rp_app's table-level UPDATE on
-- biz.risks (006) covers the new column; no grant changes.

ALTER TABLE biz.risks ADD COLUMN advice_version text;
