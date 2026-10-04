import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Check, FileText, MessageCircle, Paperclip, ThumbsUp, type LucideIcon } from "lucide-react";
import { useId } from "react";
import { Link } from "react-router";

import { api, unwrap, type Schemas } from "../api/client";
import { errorMessage } from "../api/errors";
import { isAdmin, useMe } from "../auth/context";
import { Button } from "../ui/Button";
import { useToast } from "../ui/useToast";
import styles from "./FirstSteps.module.css";

type Onboarding = Schemas["OnboardingResponse"];
type Finish = "tips" | "checklist";

const ONBOARDING_KEY = ["onboarding"] as const;

const FLAG: Record<Finish, "tips_seen" | "checklist_hidden"> = {
  tips: "tips_seen",
  checklist: "checklist_hidden",
};

/**
 * Первые шаги (ТЗ §8) на пустом экране чата: чек-лист администратору,
 * подсказки сотруднику. Что показано и что скрыто, хранит сервер — на
 * новом устройстве не всплывёт снова. Нет ответа сервера — не показываем
 * ничего: приветствие чата важнее.
 */
export function FirstSteps() {
  const me = useMe();
  const onboarding = useQuery({
    queryKey: ONBOARDING_KEY,
    queryFn: () => unwrap(api.GET("/api/v1/onboarding")),
    // Галочки ставит сервер по данным компании: документы загрузили в
    // соседней вкладке или на другой странице — вернулись, и чек-лист знает.
    staleTime: 0,
    refetchOnWindowFocus: true,
  });
  const state = onboarding.data;
  if (!state) return null;
  return isAdmin(me) ? <Checklist state={state} /> : <Tips state={state} />;
}

/** После «Скрыть» и «Понятно» блок исчезает — фокус переходит в поле вопроса. */
function focusQuestion() {
  document.getElementById("question")?.focus();
}

/** Отметить шаг на сервере. Блок прячется сразу; не вышло — возвращается с сообщением. */
function useFinish(step: Finish) {
  const queryClient = useQueryClient();
  const toast = useToast();
  return useMutation({
    mutationFn: () => unwrap(api.POST("/api/v1/onboarding/{step}", { params: { path: { step } } })),
    onMutate: async () => {
      await queryClient.cancelQueries({ queryKey: ONBOARDING_KEY });
      const previous = queryClient.getQueryData<Onboarding>(ONBOARDING_KEY);
      queryClient.setQueryData<Onboarding>(
        ONBOARDING_KEY,
        (old) => old && { ...old, [FLAG[step]]: true },
      );
      focusQuestion();
      return { previous };
    },
    onError: (err, _variables, context) => {
      if (context?.previous) queryClient.setQueryData(ONBOARDING_KEY, context.previous);
      toast.show(errorMessage(err), { tone: "error" });
    },
    onSuccess: (state) => queryClient.setQueryData(ONBOARDING_KEY, state),
  });
}

interface Step {
  key: string;
  done: boolean;
  title: string;
  to?: string;
  hint?: string;
}

function Checklist({ state }: { state: Onboarding }) {
  const titleId = useId();
  const hide = useFinish("checklist");
  const steps: Step[] = [
    {
      key: "documents",
      done: state.documents,
      title: "Загрузите документы или подключите источник",
      to: "/admin/sources/files",
    },
    { key: "people", done: state.people, title: "Пригласите сотрудников", to: "/admin/users" },
    {
      key: "question",
      done: state.question,
      title: "Задайте первый вопрос",
      hint: "— прямо здесь, в поле ниже",
    },
  ];
  const done = steps.filter((step) => step.done).length;
  if (state.checklist_hidden || done === steps.length) return null;

  return (
    <section className={styles.box} aria-labelledby={titleId}>
      <div className={styles.head}>
        <h3 id={titleId} className={styles.title}>
          Первые шаги
        </h3>
        <span className={styles.count}>
          {done} из {steps.length}
        </span>
        <Button
          variant="link"
          size="xs"
          className={styles.hide}
          aria-label="Скрыть первые шаги"
          onClick={() => hide.mutate()}
        >
          Скрыть
        </Button>
      </div>
      {/* Число уже в тексте рядом с заголовком — полоска для глаза. */}
      <div className={styles.bar} aria-hidden>
        <span style={{ width: `${(done / steps.length) * 100}%` }} />
      </div>
      <ol className={styles.steps}>
        {steps.map((step, index) => (
          <li key={step.key} className={`${styles.step} ${step.done ? styles.done : ""}`}>
            <span className={styles.mark} aria-hidden>
              {step.done ? <Check size={14} strokeWidth={2.5} /> : index + 1}
            </span>
            <span className={styles.stepText}>
              {step.to && !step.done ? <Link to={step.to}>{step.title}</Link> : step.title}
              {step.hint && !step.done ? <span className="muted"> {step.hint}</span> : null}
              <span className="visually-hidden">{step.done ? " — готово" : " — не сделано"}</span>
            </span>
          </li>
        ))}
      </ol>
    </section>
  );
}

const TIPS: { icon: LucideIcon; text: string }[] = [
  { icon: MessageCircle, text: "Спрашивайте как коллегу — обычными словами" },
  {
    icon: FileText,
    text: "В каждом ответе — ссылка на документ: нажмите, чтобы увидеть фрагмент",
  },
  { icon: Paperclip, text: "Приложите файл скрепкой — спросите по договору или приказу" },
  { icon: ThumbsUp, text: "👍 и 👎 под ответом помогают администратору улучшать документы" },
];

function Tips({ state }: { state: Onboarding }) {
  const titleId = useId();
  const seen = useFinish("tips");
  if (state.tips_seen) return null;

  return (
    <section className={styles.box} aria-labelledby={titleId}>
      <h3 id={titleId} className={styles.title}>
        Несколько советов
      </h3>
      <ul className={styles.tips}>
        {TIPS.map(({ icon: Icon, text }) => (
          <li key={text} className={styles.tip}>
            <Icon size={18} aria-hidden className={styles.tipIcon} />
            <span>{text}</span>
          </li>
        ))}
      </ul>
      <div className={styles.foot}>
        <Link to="/help">Подробнее — в «Помощи»</Link>
        <Button size="xs" onClick={() => seen.mutate()}>
          Понятно
        </Button>
      </div>
    </section>
  );
}
