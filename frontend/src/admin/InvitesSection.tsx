import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link2 } from "lucide-react";
import { useState, type SubmitEvent } from "react";

import { api, unwrap, type Schemas } from "../api/client";
import { errorMessage } from "../api/errors";
import { formatDateTime } from "../lib/format";
import { Badge } from "../ui/Badge";
import { Button } from "../ui/Button";
import { SelectField, TextField } from "../ui/Field";
import { Modal } from "../ui/Modal";
import { Notice } from "../ui/Notice";
import pageStyles from "../ui/Page.module.css";
import { Table } from "../ui/Table";
import tableStyles from "../ui/Table.module.css";
import { ConfirmDialog, SecretValue } from "./common";
import { inviteLink } from "./inviteLink";

type Invite = Schemas["InviteResponse"];

const STATUS: Record<Invite["status"], { label: string; tone: "ok" | "muted" | "error" }> = {
  active: { label: "действует", tone: "ok" },
  expired: { label: "истекла", tone: "muted" },
  revoked: { label: "отозвана", tone: "error" },
  used_up: { label: "использована", tone: "muted" },
};

/**
 * Ссылки-приглашения (решение 28.09): админ отправляет ссылку куда удобно,
 * по ней человек сам заводит учётку сотрудника и сразу входит.
 */
export function InvitesSection() {
  const queryClient = useQueryClient();
  const invites = useQuery({
    queryKey: ["invites"],
    queryFn: () => unwrap(api.GET("/api/v1/invites")),
  });
  const [creating, setCreating] = useState(false);
  const [link, setLink] = useState<string | null>(null);
  const [revoking, setRevoking] = useState<Invite | null>(null);

  return (
    <section className={pageStyles.section} aria-labelledby="invites-title">
      <div className={pageStyles.row} style={{ justifyContent: "space-between" }}>
        <h2 className={pageStyles.sectionTitle} id="invites-title">
          Ссылки-приглашения
        </h2>
        <Button size="sm" variant="ghost" onClick={() => setCreating(true)}>
          <Link2 size={16} aria-hidden /> Пригласить по ссылке
        </Button>
      </div>
      <p className="muted" style={{ marginBottom: 12 }}>
        Отправьте ссылку в рабочий чат или почтой: по ней сотрудник сам задаст почту и пароль и
        сразу окажется в компании. Любой, у кого есть ссылка, сможет присоединиться, пока она
        действует, — ограничьте срок, число людей или домен почты.
      </p>
      {invites.isError ? <Notice kind="error">{errorMessage(invites.error)}</Notice> : null}
      {invites.data && invites.data.length > 0 ? (
        <Table label="Ссылки-приглашения">
          <thead>
            <tr>
              <th>Создана</th>
              <th>Действует до</th>
              <th>Присоединились</th>
              <th>Домен почты</th>
              <th>Состояние</th>
              <th className={tableStyles.actions}>
                <span className="visually-hidden">Действия</span>
              </th>
            </tr>
          </thead>
          <tbody>
            {invites.data.map((invite) => (
              <tr key={invite.id}>
                <td className={tableStyles.nowrap}>{formatDateTime(invite.created_at)}</td>
                <td className={tableStyles.nowrap}>{formatDateTime(invite.expires_at)}</td>
                <td className="num">
                  {invite.uses} из {invite.max_uses}
                </td>
                <td>{invite.email_domain ? `@${invite.email_domain}` : "любой"}</td>
                <td>
                  <Badge tone={STATUS[invite.status].tone}>{STATUS[invite.status].label}</Badge>
                </td>
                <td className={tableStyles.actions}>
                  {invite.status === "active" ? (
                    <Button variant="danger" size="xs" onClick={() => setRevoking(invite)}>
                      Отозвать
                    </Button>
                  ) : null}
                </td>
              </tr>
            ))}
          </tbody>
        </Table>
      ) : null}

      {creating ? (
        <CreateInviteDialog
          onClose={() => setCreating(false)}
          onCreated={(created) => {
            setCreating(false);
            setLink(inviteLink(created.company_code, created.token));
          }}
        />
      ) : null}
      <Modal
        open={link !== null}
        onOpenChange={(open) => !open && setLink(null)}
        title="Ссылка-приглашение"
        description="Скопируйте и отправьте её сотрудникам. Больше она не покажется — если потеряете, создайте новую."
        footer={
          <Button size="sm" onClick={() => setLink(null)}>
            Готово
          </Button>
        }
      >
        {link ? <SecretValue value={link} copyLabel="Скопировать ссылку" /> : null}
      </Modal>
      <ConfirmDialog
        open={revoking !== null}
        onOpenChange={(open) => !open && setRevoking(null)}
        title="Отозвать ссылку?"
        description="По ней больше нельзя будет присоединиться. Уже присоединившиеся сотрудники останутся."
        confirmLabel="Отозвать"
        onConfirm={async () => {
          if (!revoking) return;
          await unwrap(
            api.DELETE("/api/v1/invites/{invite_id}", {
              params: { path: { invite_id: revoking.id } },
            }),
          );
          await queryClient.invalidateQueries({ queryKey: ["invites"] });
        }}
      />
    </section>
  );
}

function CreateInviteDialog({
  onClose,
  onCreated,
}: {
  onClose: () => void;
  onCreated: (created: Schemas["InviteCreatedResponse"]) => void;
}) {
  const queryClient = useQueryClient();
  const [ttlDays, setTtlDays] = useState("7");
  const [maxUses, setMaxUses] = useState("");
  const [domain, setDomain] = useState("");
  const create = useMutation({
    mutationFn: () =>
      unwrap(
        api.POST("/api/v1/invites", {
          body: {
            ttl_days: Number(ttlDays),
            max_uses: maxUses.trim() ? Number(maxUses) : null,
            email_domain: domain.trim() || null,
          },
        }),
      ),
    onSuccess: async (created) => {
      await queryClient.invalidateQueries({ queryKey: ["invites"] });
      onCreated(created);
    },
  });

  function submit(event: SubmitEvent) {
    event.preventDefault();
    create.mutate();
  }

  return (
    <Modal open onOpenChange={(open) => !open && onClose()} title="Пригласить по ссылке">
      <form className={pageStyles.form} onSubmit={submit}>
        {create.isError ? <Notice kind="error">{errorMessage(create.error)}</Notice> : null}
        <SelectField
          label="Ссылка действует"
          value={ttlDays}
          onChange={(e) => setTtlDays(e.target.value)}
        >
          <option value="1">1 день</option>
          <option value="7">7 дней</option>
          <option value="30">30 дней</option>
        </SelectField>
        <TextField
          label="Сколько человек может присоединиться"
          optional
          type="number"
          inputMode="numeric"
          min={1}
          max={1000}
          value={maxUses}
          onChange={(e) => setMaxUses(e.target.value)}
          hint="Пусто — по числу рабочих мест компании."
        />
        <TextField
          label="Только почта домена"
          optional
          placeholder="acme.ru"
          value={domain}
          onChange={(e) => setDomain(e.target.value)}
          hint="Например, рабочий домен компании: присоединиться с личной почтой не получится."
        />
        <div className={pageStyles.row} style={{ justifyContent: "flex-end" }}>
          <Button variant="ghost" size="sm" onClick={onClose}>
            Отмена
          </Button>
          <Button type="submit" size="sm" busy={create.isPending}>
            Создать ссылку
          </Button>
        </div>
      </form>
    </Modal>
  );
}
