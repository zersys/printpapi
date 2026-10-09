"use client";

import { useCallback, useState } from "react";
import { toast } from "sonner";
import { Dices, Loader2, Pencil, Plus, Trash2, Webhook as WebhookIcon } from "lucide-react";
import { usePoll } from "@/hooks/use-poll";
import {
  createWebhook,
  deleteWebhook,
  listWebhooks,
  updateWebhook,
  type Webhook,
  type WebhookInput,
} from "@/lib/api";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Badge } from "@/components/ui/badge";
import { Skeleton } from "@/components/ui/skeleton";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
  AlertDialogTrigger,
} from "@/components/ui/alert-dialog";
import { EmptyState, FieldLabel } from "@/components/bits";

const MAX = 5; // the server's per-org cap

// With two message types, one select covers every useful choice: both ("*") or either one.
const CHOICES: { value: string; label: string; messages: WebhookInput["messages"] }[] = [
  { value: "*", label: "All events", messages: ["*"] },
  { value: "print job state", label: "Print job state", messages: ["print job state"] },
  { value: "computer state", label: "Computer state", messages: ["computer state"] },
];

const choiceOf = (messages: WebhookInput["messages"]) =>
  messages.length === 1 && messages[0] !== "*" ? messages[0] : "*";

const message = (err: unknown) => (err instanceof Error ? err.message : String(err));

/** 32 random hex characters — a secret nobody has to invent. */
function randomSecret() {
  const bytes = new Uint8Array(16);
  crypto.getRandomValues(bytes);
  return Array.from(bytes, (b) => b.toString(16).padStart(2, "0")).join("");
}

function WebhookForm({
  initial,
  submitLabel,
  onSubmit,
}: {
  initial?: WebhookInput;
  submitLabel: string;
  onSubmit: (hook: WebhookInput) => Promise<void>;
}) {
  const [url, setUrl] = useState(initial?.url ?? "");
  const [secret, setSecret] = useState(initial?.secret ?? "");
  const [choice, setChoice] = useState(initial ? choiceOf(initial.messages) : "*");
  const [busy, setBusy] = useState(false);
  const id = initial ? "edit" : "new";

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true);
    try {
      const messages = CHOICES.find((c) => c.value === choice)!.messages;
      await onSubmit({ url: url.trim(), secret, messages });
      if (!initial) {
        setUrl("");
        setSecret("");
        setChoice("*");
      }
    } catch {
      /* the caller already showed the error; keep what was typed */
    } finally {
      setBusy(false);
    }
  }

  return (
    <form onSubmit={submit} className="space-y-3">
      <div>
        <FieldLabel htmlFor={`${id}-url`}>Target URL</FieldLabel>
        <Input
          id={`${id}-url`}
          type="url"
          required
          className="mt-1.5 font-mono"
          placeholder="https://yourapp.example/printpapi/webhook"
          value={url}
          onChange={(e) => setUrl(e.target.value)}
        />
      </div>
      <div className="flex flex-wrap items-end gap-2">
        <div className="min-w-56 flex-1">
          <FieldLabel htmlFor={`${id}-secret`}>Secret</FieldLabel>
          <div className="mt-1.5 flex gap-2">
            <Input
              id={`${id}-secret`}
              required
              autoComplete="off"
              className="font-mono"
              placeholder="sent as X-Webhook-Secret"
              value={secret}
              onChange={(e) => setSecret(e.target.value)}
            />
            <Button
              type="button"
              variant="outline"
              size="icon"
              aria-label="Generate a secret"
              onClick={() => setSecret(randomSecret())}
            >
              <Dices />
            </Button>
          </div>
        </div>
        <div>
          <FieldLabel>Messages</FieldLabel>
          <Select value={choice} onValueChange={setChoice}>
            <SelectTrigger className="mt-1.5 w-44">
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              {CHOICES.map((c) => (
                <SelectItem key={c.value} value={c.value}>
                  {c.label}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
        </div>
        <Button type="submit" variant={initial ? "brand" : "outline"} disabled={busy}>
          {busy ? <Loader2 className="animate-spin" /> : initial ? <Pencil /> : <Plus />}
          {submitLabel}
        </Button>
      </div>
    </form>
  );
}

/**
 * The org's account webhooks (printapi-compatible). Mount it with `key={orgId}`: usePoll fetches once
 * per mount, so switching orgs must remount rather than re-render.
 */
export function WebhooksCard({ orgId }: { orgId: number }) {
  const { data: hooks, loading, refresh } = usePoll(
    useCallback(() => listWebhooks(orgId), [orgId]),
    0,
  );
  const [editing, setEditing] = useState<Webhook | null>(null);

  async function add(hook: WebhookInput) {
    try {
      await createWebhook(orgId, hook);
      toast.success("Webhook added");
      refresh();
    } catch (err) {
      toast.error("Could not add the webhook", { description: message(err) });
      throw err; // keep the form filled in
    }
  }

  async function save(hook: WebhookInput) {
    if (!editing) return;
    try {
      await updateWebhook(orgId, editing.id, hook);
      toast.success("Webhook saved");
      setEditing(null);
      refresh();
    } catch (err) {
      toast.error("Could not save the webhook", { description: message(err) });
    }
  }

  async function remove(hook: Webhook) {
    try {
      await deleteWebhook(orgId, hook.id);
      toast.success("Webhook removed");
      refresh();
    } catch (err) {
      toast.error("Could not remove the webhook", { description: message(err) });
    }
  }

  const list = hooks ?? [];

  return (
    <Card>
      <CardHeader>
        <CardTitle className="flex items-center gap-2">
          <WebhookIcon className="size-4" /> Webhooks
        </CardTitle>
        <CardDescription>
          The server POSTs <code>computer state</code> and <code>print job state</code> events to
          these URLs as they happen — a JSON array of events, with your secret in
          the <code>X-Webhook-Secret</code> header so you can tell the request is ours. A failed
          delivery is retried once after 5 seconds. Up to {MAX} per org.
        </CardDescription>
      </CardHeader>
      <CardContent className="space-y-4">
        {loading && !hooks ? (
          <Skeleton className="h-14 w-full" />
        ) : list.length === 0 ? (
          <EmptyState
            icon={WebhookIcon}
            title="No webhooks yet"
            hint="Add one below to hear about every print job and every computer going online or offline."
          />
        ) : (
          <ul className="divide-y rounded-lg border">
            {list.map((h) => (
              <li key={h.id} className="flex flex-wrap items-center gap-3 p-3">
                <div className="min-w-0 flex-1 space-y-1">
                  <div className="truncate font-mono text-sm" title={h.url}>
                    {h.url}
                  </div>
                  <div className="flex flex-wrap items-center gap-2 text-xs text-muted-foreground">
                    {h.messages.map((m) => (
                      <Badge key={m} variant="secondary">
                        {m === "*" ? "all events" : m}
                      </Badge>
                    ))}
                    <span>
                      {h.received_events} events · {h.successful_requests} delivered ·{" "}
                      {h.failed_requests} failed · {h.dropped_events} dropped
                    </span>
                  </div>
                </div>
                <Button
                  variant="ghost"
                  size="icon"
                  aria-label={`Edit ${h.url}`}
                  onClick={() => setEditing(h)}
                >
                  <Pencil />
                </Button>
                <AlertDialog>
                  <AlertDialogTrigger asChild>
                    <Button variant="ghost" size="icon" aria-label={`Remove ${h.url}`}>
                      <Trash2 />
                    </Button>
                  </AlertDialogTrigger>
                  <AlertDialogContent>
                    <AlertDialogHeader>
                      <AlertDialogTitle>Remove this webhook?</AlertDialogTitle>
                      <AlertDialogDescription>
                        No new events go to {h.url}. Events already queued for it are still
                        delivered.
                      </AlertDialogDescription>
                    </AlertDialogHeader>
                    <AlertDialogFooter>
                      <AlertDialogCancel>Keep it</AlertDialogCancel>
                      <AlertDialogAction onClick={() => remove(h)}>Remove</AlertDialogAction>
                    </AlertDialogFooter>
                  </AlertDialogContent>
                </AlertDialog>
              </li>
            ))}
          </ul>
        )}

        {list.length < MAX ? (
          <WebhookForm submitLabel="Add" onSubmit={add} />
        ) : (
          <p className="text-sm text-muted-foreground">
            This org has the maximum of {MAX} webhooks — remove one to add another.
          </p>
        )}
      </CardContent>

      <Dialog open={editing !== null} onOpenChange={(open) => !open && setEditing(null)}>
        <DialogContent className="sm:max-w-xl">
          <DialogHeader>
            <DialogTitle>Edit webhook</DialogTitle>
            <DialogDescription>
              Events already queued keep going to the old URL with the old secret.
            </DialogDescription>
          </DialogHeader>
          {editing && (
            <WebhookForm
              key={editing.id}
              initial={editing}
              submitLabel="Save"
              onSubmit={save}
            />
          )}
        </DialogContent>
      </Dialog>
    </Card>
  );
}
