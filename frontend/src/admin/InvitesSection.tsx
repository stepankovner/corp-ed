import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { UserPlus } from "lucide-react";
import { useId, useState, type SubmitEvent } from "react";

import { api, unwrap, type Schemas } from "../api/client";
import { errorMessage } from "../api/errors";
import { formatDateTime } from "../lib/format";
import { Badge, type Tone } from "../ui/Badge";
import { Button } from "../ui/Button";
import { Checkbox, SelectField, TextField } from "../ui/Field";
import { Modal } from "../ui/Modal";
import { Notice } from "../ui/Notice";
import pageStyles from "../ui/Page.module.css";
import { Table } from "../ui/Table";
import tableStyles from "../ui/Table.module.css";
import styles from "./Admin.module.css";
import { ConfirmDialog, CopyButton, SecretValue } from "./common";
import { inviteLink } from "./inviteLink";

type Invite = Schemas["InviteResponse"];
type Created = Schemas["InviteCreatedResponse"];

const STATUS: Record<Invite["status"], { label: string; tone: Tone }> = {
  active: { label: "действует", tone: "ok" },
  expired: { label: "истекло", tone: "muted" },
  revoked: { label: "отозвано", tone: "error" },
  used_up: { label: "использовано", tone: "muted" },
};

/**
 * Приглашения компании (ТЗ §2, решения 28.09 и 03.10): ссылка и короткий
 * код для диктовки. По ним вступают своей учёткой kronto — пока
 * приглашение действует, это может любой, у кого оно есть.
 */
export function InvitesSection() {
  const queryClient = useQueryClient();
  const invites = useQuery({
    queryKey: ["invites"],
    queryFn: () => unwrap(api.GET("/api/v1/invites")),
  });
  const [revoking, setRevoking] = useState<Invite | null>(null);

  return (
    <section className={pageStyles.section} aria-labelledby="invites-title">
      <h2 className={pageStyles.sectionTitle} id="invites-title">
        Приглашения
      </h2>
      <p className="muted" style={{ marginBottom: 12 }}>
        Ссылку отправляют в рабочий чат или почтой, код диктуют. Пока приглашение действует, по нему
        может вступить любой, у кого оно есть, — ограничьте срок, число людей или домен почты, а для
        большого общего чата включите одобрение. Сами ссылки и коды здесь не хранятся.
      </p>
      {invites.isError ? <Notice kind="error">{errorMessage(invites.error)}</Notice> : null}
      {invites.data && invites.data.length === 0 ? (
        <p className="muted">Приглашений пока нет.</p>
      ) : null}
      {invites.data && invites.data.length > 0 ? (
        <Table label="Приглашения" rowTitle={false}>
          <thead>
            <tr>
              <th>Создано</th>
              <th>Действует до</th>
              <th>Вступили</th>
              <th>Домен почты</th>
              <th>Вступление</th>
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
                  {invite.requires_approval ? (
                    <Badge tone="warn">с одобрением</Badge>
                  ) : (
                    <span className="muted">сразу</span>
                  )}
                </td>
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

      <ConfirmDialog
        open={revoking !== null}
        onOpenChange={(open) => !open && setRevoking(null)}
        title="Отозвать приглашение?"
        description="Ни по ссылке, ни по коду больше нельзя будет вступить. Уже вступившие останутся в компании."
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

/**
 * «Пригласить» — в шапке страницы: учёток админ больше не заводит, это
 * единственный способ добавить человека. Ссылка и код показываются один раз.
 */
export function InviteButton() {
  const [creating, setCreating] = useState(false);
  const [created, setCreated] = useState<Created | null>(null);
  return (
    <>
      <Button size="sm" onClick={() => setCreating(true)}>
        <UserPlus size={16} aria-hidden /> Пригласить
      </Button>
      {creating ? (
        <CreateInviteDialog
          onClose={() => setCreating(false)}
          onCreated={(result) => {
            setCreating(false);
            setCreated(result);
          }}
        />
      ) : null}
      <Modal
        open={created !== null}
        onOpenChange={(open) => !open && setCreated(null)}
        title="Приглашение"
        description="Ссылка и код показываются один раз — если потеряете, создайте новое приглашение."
        footer={
          <Button size="sm" onClick={() => setCreated(null)}>
            Готово
          </Button>
        }
      >
        {created ? <InviteSecrets created={created} /> : null}
      </Modal>
    </>
  );
}

function InviteSecrets({ created }: { created: Created }) {
  return (
    <div className={pageStyles.stack}>
      <div>
        <p className={styles.legend}>Ссылка — для рабочего чата или письма</p>
        <SecretValue
          value={inviteLink(created.token)}
          copyLabel="Скопировать ссылку"
          testId="invite-link"
        />
      </div>
      <div>
        <p className={styles.legend}>Код — чтобы продиктовать</p>
        <div className={styles.inviteCode}>
          <code data-testid="invite-code">{created.code}</code>
          <CopyButton value={created.code} label="Скопировать код" />
        </div>
      </div>
      <ul className={styles.howTo}>
        <li>
          Ссылка открывает страницу вступления: человек входит в свою учётку kronto или
          регистрируется и попадает в компанию.
        </li>
        <li>
          Код вводят на главной kronto в поле «Вступить по коду» или в «Настройки → Компании».
          Регистр и дефис не важны.
        </li>
        {created.invite.requires_approval ? (
          <li>
            Вступившие появятся в «Ждут одобрения» на этой странице: доступ откроется, когда вы
            одобрите заявку.
          </li>
        ) : null}
      </ul>
    </div>
  );
}

function CreateInviteDialog({
  onClose,
  onCreated,
}: {
  onClose: () => void;
  onCreated: (created: Created) => void;
}) {
  const queryClient = useQueryClient();
  const approvalHint = useId();
  const [ttlDays, setTtlDays] = useState("7");
  const [maxUses, setMaxUses] = useState("");
  const [domain, setDomain] = useState("");
  const [approval, setApproval] = useState(false);
  const create = useMutation({
    mutationFn: () =>
      unwrap(
        api.POST("/api/v1/invites", {
          body: {
            ttl_days: Number(ttlDays),
            max_uses: maxUses.trim() ? Number(maxUses) : null,
            email_domain: domain.trim() || null,
            requires_approval: approval,
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
    <Modal
      open
      onOpenChange={(open) => !open && onClose()}
      title="Пригласить в компанию"
      description="Получите ссылку и короткий код: по ним сотрудники вступают своей учёткой kronto."
    >
      <form className={pageStyles.form} onSubmit={submit}>
        {create.isError ? <Notice kind="error">{errorMessage(create.error)}</Notice> : null}
        <SelectField
          label="Приглашение действует"
          value={ttlDays}
          onChange={(e) => setTtlDays(e.target.value)}
        >
          <option value="1">1 день</option>
          <option value="7">7 дней</option>
          <option value="30">30 дней</option>
        </SelectField>
        <TextField
          label="Сколько человек может вступить"
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
          hint="Например, рабочий домен компании: с личной почтой вступить не получится."
        />
        <div>
          <Checkbox
            label="Требовать одобрения администратора"
            checked={approval}
            onChange={(e) => setApproval(e.target.checked)}
            aria-describedby={approvalHint}
          />
          <p className={styles.checkHint} id={approvalHint}>
            Вступивший получит доступ, только когда администратор одобрит заявку на этой странице.
            Пригодится, если ссылка уйдёт в большой общий чат.
          </p>
        </div>
        <div className={pageStyles.row} style={{ justifyContent: "flex-end" }}>
          <Button variant="ghost" size="sm" onClick={onClose}>
            Отмена
          </Button>
          <Button type="submit" size="sm" busy={create.isPending}>
            Создать приглашение
          </Button>
        </div>
      </form>
    </Modal>
  );
}
