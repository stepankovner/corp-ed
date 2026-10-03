import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useId, useState, type SubmitEvent } from "react";

import { api, unwrap } from "../api/client";
import { errorMessage } from "../api/errors";
import { useAuth } from "../auth/context";
import { Button } from "../ui/Button";
import { SelectField, TextField } from "../ui/Field";
import { Modal } from "../ui/Modal";
import { Notice } from "../ui/Notice";
import { useToast } from "../ui/useToast";
import { DEPARTMENTS_KEY, PEOPLE_KEY } from "./keys";

export interface WorkTarget {
  memberId: string;
  name: string;
  position: string | null;
  departmentId: string | null;
}

/**
 * Должность и отдел коллеги — правит администратор (ТЗ §4: «заполняет
 * сам человек, админ может поправить»). Правка попадает в журнал.
 */
export function WorkEditDialog({ target, onClose }: { target: WorkTarget; onClose: () => void }) {
  const formId = useId();
  const { reloadMe } = useAuth();
  const toast = useToast();
  const queryClient = useQueryClient();
  const [position, setPosition] = useState(target.position ?? "");
  const [department, setDepartment] = useState(target.departmentId ?? "");
  const departments = useQuery({
    queryKey: DEPARTMENTS_KEY,
    queryFn: () => unwrap(api.GET("/api/v1/departments")),
  });

  const save = useMutation({
    mutationFn: () =>
      unwrap(
        api.PATCH("/api/v1/people/{member_id}", {
          params: { path: { member_id: target.memberId } },
          body: {
            position: position.split(/\s+/).filter(Boolean).join(" ") || null,
            department_id: department || null,
          },
        }),
      ),
    onSuccess: async () => {
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: PEOPLE_KEY }),
        queryClient.invalidateQueries({ queryKey: DEPARTMENTS_KEY }),
        queryClient.invalidateQueries({ queryKey: ["users"] }),
        // Администратор поправил себя — в профиле тоже новое.
        reloadMe(),
      ]);
      toast.show("Сохранено");
      onClose();
    },
  });

  function submit(event: SubmitEvent) {
    event.preventDefault();
    save.mutate();
  }

  return (
    <Modal
      open
      onOpenChange={(open) => !open && !save.isPending && onClose()}
      title="Должность и отдел"
      description={target.name}
      footer={
        <>
          <Button variant="ghost" size="sm" onClick={onClose} disabled={save.isPending}>
            Отмена
          </Button>
          <Button type="submit" form={formId} size="sm" busy={save.isPending}>
            Сохранить
          </Button>
        </>
      }
    >
      <form id={formId} onSubmit={submit} style={{ display: "grid", gap: "var(--s-4)" }}>
        {save.isError ? <Notice kind="error">{errorMessage(save.error)}</Notice> : null}
        <TextField
          label="Должность"
          optional
          maxLength={100}
          value={position}
          onChange={(e) => setPosition(e.target.value)}
          autoFocus
        />
        <SelectField
          label="Отдел"
          optional
          value={department}
          onChange={(e) => setDepartment(e.target.value)}
          disabled={departments.isPending}
        >
          <option value="">Не выбран</option>
          {(departments.data ?? []).map((item) => (
            <option key={item.id} value={item.id}>
              {item.name}
            </option>
          ))}
        </SelectField>
      </form>
    </Modal>
  );
}
