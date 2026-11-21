import { useTranslation } from "react-i18next";
import { Badge, CopyValue, Fact, FieldList, TechnicalDetails } from "../components/Primitives";
import { formatDateTime } from "../i18n";
import type { NativeCoworkerSession } from "../types";

export function CoworkerNativeWork({
  sessions,
  dryRun,
}: {
  sessions: NativeCoworkerSession[];
  dryRun: boolean;
}) {
  const { t } = useTranslation();
  return (
    <section className="queue-panel coworker-native-work" aria-label={t("coworker.nativeWork")}>
      <h2>{t("coworker.nativeWork")}</h2>
      {sessions.length ? (
        <div className="work-main">
          {sessions.map((session) => {
            const title = session.session_id == null
              ? t("coworker.watchOnlyTopic")
              : t("coworker.nativeSession", { id: session.session_id });
            const { handoff } = session;
            return (
              <div
                className="detail-panel"
                key={session.session_id ?? JSON.stringify([
                  session.app_id, session.chat_id, session.topic_key,
                ])}
              >
                <h3>{title}</h3>
                <TechnicalDetails summary={t("coworker.sessionReference")}>
                  <p className="detail-note">{session.chat_id} · {session.topic_key} · {session.app_id}</p>
                </TechnicalDetails>
                <FieldList>
                  {session.deliveries.map((delivery) => (
                    <Fact
                      key={delivery.id}
                      label={t("coworker.deliveryReview", { id: delivery.id })}
                      value={
                        <>
                          <Badge tone="warning">
                            {t(`coworker.deliveryPurpose.${delivery.purpose}`, {
                              defaultValue: delivery.purpose,
                            })}
                          </Badge>
                          <span className="detail-note"> {formatDateTime(delivery.updated_at)}</span>
                          {delivery.error ? <code className="coworker-native-line">{delivery.error}</code> : null}
                          <TechnicalDetails summary={t("coworker.deliveryEvidence")}>
                            <FieldList>
                              <Fact label={t("coworker.deliveryTarget")} value={<code>{delivery.target_message_id}</code>} />
                              {delivery.sent_message_id ? <Fact label={t("coworker.deliveryReceipt")} value={<code>{delivery.sent_message_id}</code>} /> : null}
                              {delivery.receipt_thread_id ? <Fact label={t("coworker.deliveryThread")} value={<code>{delivery.receipt_thread_id}</code>} /> : null}
                            </FieldList>
                          </TechnicalDetails>
                        </>
                      }
                    />
                  ))}
                  {handoff ? (
                    <Fact
                      label={t("coworker.handoff")}
                      value={
                        <>
                          <Badge tone={handoff.status === "ready" ? "success" : "neutral"}>
                            {t(`coworker.handoff.${handoff.status}`, { defaultValue: handoff.status })}
                          </Badge>
                          <span className="detail-note"> {formatDateTime(handoff.updated_at)}</span>
                          {handoff.error ? <code className="coworker-native-line">{handoff.error}</code> : null}
                          {!dryRun && handoff.status === "ready" && handoff.operator_command ? (
                            <span className="coworker-native-line">
                              <CopyValue value={handoff.operator_command} label={t("coworker.handoffCommand")} />
                            </span>
                          ) : null}
                          {handoff.status === "ready" && dryRun ? (
                            <span className="coworker-native-line">{t("coworker.handoffDryRun")}</span>
                          ) : null}
                        </>
                      }
                    />
                  ) : null}
                  {session.watch_expires_at ? (
                    <Fact
                      label={t("coworker.watchExpiry")}
                      value={formatDateTime(session.watch_expires_at)}
                    />
                  ) : null}
                </FieldList>
              </div>
            );
          })}
          {sessions.some((session) => session.deliveries.length) ? (
            <p className="detail-note">{t("coworker.deliveryReviewHint")}</p>
          ) : null}
        </div>
      ) : (
        <p className="detail-note">{t("coworker.nativeEmpty")}</p>
      )}
    </section>
  );
}
