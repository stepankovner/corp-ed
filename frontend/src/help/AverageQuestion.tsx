import { useQuery } from "@tanstack/react-query";

import { api, unwrap } from "../api/client";
import { averageQuestionNote } from "../lib/credits";

/**
 * «В среднем один вопрос — около N кредитов»: N — из настроек сервера
 * (BILLING_AVG_CREDITS_PER_QUESTION), его отдаёт /usage администратору.
 * Нет ответа — без числа.
 */
export function AverageQuestion() {
  const usage = useQuery({
    queryKey: ["usage"],
    queryFn: () => unwrap(api.GET("/api/v1/usage")),
    retry: false,
  });
  const avg = usage.data?.avg_credits_per_question;
  return (
    <>{avg ? averageQuestionNote(avg) : "Длинные вопросы и ответы списывают больше кредитов."}</>
  );
}
