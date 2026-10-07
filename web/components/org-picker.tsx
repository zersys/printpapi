"use client";

import { useState } from "react";
import { toast } from "sonner";
import { Building2, Loader2, Plus } from "lucide-react";
import { createOrg, type Org } from "@/lib/api";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { FieldLabel } from "@/components/bits";

/**
 * Root only. The bootstrap token belongs to no org, so every org-scoped page asks it which org to
 * act in — and offers to create one, since a fresh server has nothing but the seeded default.
 */
export function OrgPicker({
  orgs,
  value,
  onChange,
  onCreated,
  description,
}: {
  orgs: Org[] | undefined;
  value: number | null;
  onChange: (id: number) => void;
  /** Called with the new org's id after it is created; the caller refreshes its list. */
  onCreated: (id: number) => void;
  description: string;
}) {
  const [name, setName] = useState("");
  const [busy, setBusy] = useState(false);

  async function create(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true);
    try {
      const org = await createOrg(name.trim());
      toast.success(`Created ${org.name}`);
      setName("");
      onCreated(org.id);
    } catch (err) {
      toast.error("Could not create the org", {
        description: err instanceof Error ? err.message : String(err),
      });
    } finally {
      setBusy(false);
    }
  }

  return (
    <Card>
      <CardHeader>
        <CardTitle className="flex items-center gap-2">
          <Building2 className="size-4" /> Org
        </CardTitle>
        <CardDescription>{description}</CardDescription>
      </CardHeader>
      <CardContent className="flex flex-wrap items-end gap-2">
        <div>
          <FieldLabel>Act in</FieldLabel>
          <Select
            value={value === null ? undefined : String(value)}
            onValueChange={(v) => onChange(Number(v))}
          >
            <SelectTrigger className="mt-1.5 w-64">
              <SelectValue placeholder={orgs?.length ? "Pick an org" : "No orgs yet"} />
            </SelectTrigger>
            <SelectContent>
              {(orgs ?? []).map((o) => (
                <SelectItem key={o.id} value={String(o.id)}>
                  {o.name} <span className="text-muted-foreground">#{o.id}</span>
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
        </div>
        <form onSubmit={create} className="flex min-w-64 flex-1 items-end gap-2">
          <div className="flex-1">
            <FieldLabel htmlFor="org_name">New org</FieldLabel>
            <Input
              id="org_name"
              required
              autoComplete="off"
              className="mt-1.5"
              placeholder="Acme Shop"
              value={name}
              onChange={(e) => setName(e.target.value)}
            />
          </div>
          <Button type="submit" variant="outline" disabled={busy || !name.trim()}>
            {busy ? <Loader2 className="animate-spin" /> : <Plus />}
            Create
          </Button>
        </form>
      </CardContent>
    </Card>
  );
}
