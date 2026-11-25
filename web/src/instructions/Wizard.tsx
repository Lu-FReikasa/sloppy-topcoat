import { useEffect, useMemo, useState } from "react";
import { useTranslation } from "react-i18next";
import type { ApiClient } from "../api/client";
import { Dialog } from "../components/Dialog";
import { describeError, toActionError } from "../errors";
import type { Questionnaire, QuestionnaireAnswers, QuestionnairePreview } from "../types";
import { announceInstructionsChange, pickLang } from "./personaUi";

interface Props {
  api: ApiClient;
  onClose: () => void;
  /** The persona was created (the user confirmed) or the wizard was put away (later / skipped). */
  onDone: () => void;
}

/** The first-start questionnaire: three scenes (the same answer in three tones, or "none fits"),
 *  six short questions with defaults, and a preview ("This is how I would sound") to confirm.
 *  Never a compulsion: "later" and "skip" are always there. The scenes, answers and questions are
 *  a file of the hub (German and English), not code. */
export function Wizard({ api, onClose, onDone }: Props) {
  const { t, i18n } = useTranslation();
  const lang = pickLang(i18n.resolvedLanguage);
  const [q, setQ] = useState<Questionnaire | null>(null);
  const [step, setStep] = useState(0);
  const [scenes, setScenes] = useState<Record<string, string>>({});
  const [short, setShort] = useState<Record<string, string>>({});
  const [preview, setPreview] = useState<QuestionnairePreview | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    api.questionnaire().then(
      (loaded) => {
        setQ(loaded);
        setShort(Object.fromEntries(loaded.questions.map((x) => [x.id, x.default])));
      },
      (err: unknown) => setError(describeError(toActionError(err), t, i18n)),
    );
  }, [api, t, i18n]);

  const answers = useMemo<QuestionnaireAnswers>(() => ({ scenes, ...short }), [scenes, short]);
  const sceneCount = q?.scenes.length ?? 3;
  const onPreview = q !== null && step === sceneCount + 1; // after the scenes and the questions

  useEffect(() => {
    if (!q || step !== sceneCount + 1) return;
    let alive = true;
    api.previewQuestionnaire(answers).then(
      (p) => alive && setPreview(p),
      (err: unknown) => alive && setError(describeError(toActionError(err), t, i18n)),
    );
    return () => {
      alive = false;
    };
  }, [api, q, step, sceneCount, answers, t, i18n]);

  async function away(state: "later" | "skipped") {
    try {
      await api.setWizardState(state);
    } catch (err) {
      setError(describeError(toActionError(err), t, i18n));
      return;
    }
    announceInstructionsChange();
    onDone();
    onClose();
  }

  async function confirm() {
    setBusy(true);
    try {
      await api.confirmQuestionnaire(answers);
      announceInstructionsChange();
      onDone();
      onClose();
    } catch (err) {
      setError(describeError(toActionError(err), t, i18n));
    } finally {
      setBusy(false);
    }
  }

  const scene = q && step < sceneCount ? q.scenes[step] : undefined;
  const title = t("persona.wizard.title");

  return (
    <Dialog title={title} onClose={onClose}>
      <div className="wizard" data-testid="wizard">
        {error && (
          <p className="error" role="alert">
            {error}
          </p>
        )}
        {!q && !error && <p className="muted">{t("app.loading")}</p>}
        {q && (
          <p className="muted small" data-testid="wizard-step">
            {t("persona.wizard.step", {
              n: Math.min(step + 1, sceneCount + 2),
              total: sceneCount + 2,
            })}
          </p>
        )}
        {scene && (
          <fieldset className="wizard-scene">
            <legend>
              {t("persona.wizard.scene", { n: step + 1 })}: „{scene.question[lang]}“
            </legend>
            <p className="muted small">{t("persona.wizard.sceneHint")}</p>
            {scene.answers.map((a) => (
              <label key={a.id} className="wizard-answer check">
                <input
                  type="radio"
                  name={`scene-${scene.id}`}
                  checked={scenes[scene.id] === a.id}
                  onChange={() => setScenes({ ...scenes, [scene.id]: a.id })}
                />
                <span>{a.text[lang]}</span>
              </label>
            ))}
            <label className="wizard-answer check">
              <input
                type="radio"
                name={`scene-${scene.id}`}
                checked={scenes[scene.id] === "none"}
                onChange={() => setScenes({ ...scenes, [scene.id]: "none" })}
              />
              <span>{t("persona.wizard.noneFits")}</span>
            </label>
          </fieldset>
        )}
        {q && step === sceneCount && (
          <div className="stack">
            <p>{t("persona.wizard.questionsHint")}</p>
            {q.questions.map((question) => (
              <label key={question.id} className="picker wizard-question">
                <span className="fact-label">{question.text[lang]}</span>
                <select
                  value={short[question.id] ?? question.default}
                  aria-label={question.text[lang]}
                  onChange={(e) => setShort({ ...short, [question.id]: e.target.value })}
                >
                  {question.options.map((o) => (
                    <option key={o.value} value={o.value}>
                      {o.label[lang]}
                    </option>
                  ))}
                </select>
              </label>
            ))}
          </div>
        )}
        {onPreview && (
          <div className="stack" data-testid="wizard-preview">
            <p>{t("persona.wizard.previewHint")}</p>
            {preview ? (
              <>
                <blockquote className="wizard-sample">{preview.samples[0]}</blockquote>
                <blockquote className="wizard-sample">{preview.samples[1]}</blockquote>
                <details>
                  <summary>{t("persona.showText")}</summary>
                  <pre className="plain-pre persona-text">{preview.text}</pre>
                </details>
              </>
            ) : (
              <p className="muted">{t("app.loading")}</p>
            )}
            <p className="muted small">{t("persona.wizard.changeLater")}</p>
          </div>
        )}
        <div className="row-actions wizard-actions">
          {step > 0 && (
            <button type="button" onClick={() => setStep(step - 1)}>
              {t("persona.wizard.back")}
            </button>
          )}
          {q && step <= sceneCount && (
            <button
              type="button"
              className="primary"
              onClick={() => setStep(step + 1)}
              disabled={scene ? scenes[scene.id] === undefined : false}
            >
              {t("persona.wizard.next")}
            </button>
          )}
          {onPreview && (
            <button
              type="button"
              className="primary"
              disabled={busy || !preview}
              onClick={() => void confirm()}
            >
              {t("persona.wizard.confirm")}
            </button>
          )}
          <span className="spacer" />
          <button type="button" onClick={() => void away("later")}>
            {t("persona.wizard.later")}
          </button>
          <button type="button" onClick={() => void away("skipped")}>
            {t("persona.wizard.skip")}
          </button>
        </div>
      </div>
    </Dialog>
  );
}
