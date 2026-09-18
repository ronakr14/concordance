// Radix-backed overlays: dialog, side sheet, dropdown menu, tooltip, checkbox,
// tabs. Radix supplies focus trapping, escape handling, aria wiring and
// portal layering; these wrappers only supply the look.

import { Check, Minus, X } from "lucide-react";
import { Checkbox as CheckboxPrimitive, Dialog, DropdownMenu, Tabs as TabsPrimitive, Tooltip } from "radix-ui";
import type { ComponentProps, ReactNode } from "react";

import { cn } from "@/lib/utils";

// --- dialog -------------------------------------------------------------------

export const DialogRoot = Dialog.Root;
export const DialogTrigger = Dialog.Trigger;
export const DialogClose = Dialog.Close;

export function DialogContent({
  title,
  description,
  children,
  className,
  side,
}: {
  title: ReactNode;
  description?: ReactNode;
  children: ReactNode;
  className?: string;
  /** A side sheet instead of a centred dialog. */
  side?: "right";
}) {
  return (
    <Dialog.Portal>
      <Dialog.Overlay className="fixed inset-0 z-40 bg-black/40" />
      <Dialog.Content
        className={cn(
          "fixed z-50 bg-surface text-ink shadow-xl ring-1 ring-border focus:outline-none",
          side === "right"
            ? "inset-y-0 right-0 flex w-[min(640px,100vw)] flex-col"
            : "left-1/2 top-1/2 w-[min(520px,calc(100vw-2rem))] -translate-x-1/2 -translate-y-1/2 rounded-lg",
          className,
        )}
      >
        <div className="flex items-start justify-between gap-3 border-b px-5 py-3.5">
          <div>
            <Dialog.Title className="text-base font-semibold">{title}</Dialog.Title>
            {description ? (
              <Dialog.Description className="mt-0.5 text-sm text-muted">{description}</Dialog.Description>
            ) : (
              <Dialog.Description className="sr-only">{title}</Dialog.Description>
            )}
          </div>
          <Dialog.Close className="rounded-md p-1 text-muted hover:bg-surface-2 hover:text-ink" aria-label="Close">
            <X className="size-4" />
          </Dialog.Close>
        </div>
        <div className={cn(side === "right" ? "min-h-0 flex-1 overflow-y-auto" : "", "px-5 py-4")}>{children}</div>
      </Dialog.Content>
    </Dialog.Portal>
  );
}

// --- dropdown menu ------------------------------------------------------------

export const MenuRoot = DropdownMenu.Root;
export const MenuTrigger = DropdownMenu.Trigger;

export function MenuContent({ children, className }: { children: ReactNode; className?: string }) {
  return (
    <DropdownMenu.Portal>
      <DropdownMenu.Content
        align="end"
        sideOffset={4}
        className={cn("z-50 min-w-44 rounded-md bg-surface p-1 shadow-lg ring-1 ring-border", className)}
      >
        {children}
      </DropdownMenu.Content>
    </DropdownMenu.Portal>
  );
}

export function MenuLabel({ children }: { children: ReactNode }) {
  return <DropdownMenu.Label className="px-2 py-1 text-xs font-medium text-muted">{children}</DropdownMenu.Label>;
}

export function MenuItem({ className, ...props }: ComponentProps<typeof DropdownMenu.Item>) {
  return (
    <DropdownMenu.Item
      className={cn(
        "flex cursor-default items-center gap-2 rounded-sm px-2 py-1.5 text-sm outline-none select-none data-[highlighted]:bg-surface-2 [&_svg]:size-4",
        className,
      )}
      {...props}
    />
  );
}

export function MenuCheckItem({ children, ...props }: ComponentProps<typeof DropdownMenu.CheckboxItem>) {
  return (
    <DropdownMenu.CheckboxItem
      className="flex cursor-default items-center gap-2 rounded-sm px-2 py-1.5 text-sm outline-none select-none data-[highlighted]:bg-surface-2"
      {...props}
    >
      <span className="grid size-4 place-items-center">
        <DropdownMenu.ItemIndicator>
          <Check className="size-3.5" />
        </DropdownMenu.ItemIndicator>
      </span>
      {children}
    </DropdownMenu.CheckboxItem>
  );
}

export function MenuSeparator() {
  return <DropdownMenu.Separator className="my-1 h-px bg-border" />;
}

// --- tooltip ------------------------------------------------------------------

export const TooltipProvider = Tooltip.Provider;

export function Tip({ content, children, side = "top" }: { content: ReactNode; children: ReactNode; side?: "top" | "bottom" | "left" | "right" }) {
  if (!content) return children;
  return (
    <Tooltip.Root>
      <Tooltip.Trigger asChild>{children}</Tooltip.Trigger>
      <Tooltip.Portal>
        <Tooltip.Content
          side={side}
          sideOffset={4}
          className="z-50 max-w-72 rounded-md bg-ink px-2 py-1 text-xs text-page shadow-md"
        >
          {content}
        </Tooltip.Content>
      </Tooltip.Portal>
    </Tooltip.Root>
  );
}

// --- checkbox -----------------------------------------------------------------

export function Checkbox({
  checked,
  onCheckedChange,
  className,
  ...props
}: Omit<ComponentProps<typeof CheckboxPrimitive.Root>, "checked"> & { checked: boolean | "indeterminate" }) {
  return (
    <CheckboxPrimitive.Root
      checked={checked}
      onCheckedChange={onCheckedChange}
      className={cn(
        "grid size-4 shrink-0 place-items-center rounded-sm bg-surface ring-1 ring-border-strong data-[state=checked]:bg-accent data-[state=checked]:ring-accent data-[state=indeterminate]:bg-accent data-[state=indeterminate]:ring-accent",
        className,
      )}
      {...props}
    >
      <CheckboxPrimitive.Indicator className="text-accent-fg">
        {checked === "indeterminate" ? <Minus className="size-3" /> : <Check className="size-3" />}
      </CheckboxPrimitive.Indicator>
    </CheckboxPrimitive.Root>
  );
}

// --- tabs ---------------------------------------------------------------------

export const TabsRoot = TabsPrimitive.Root;
export const TabsContent = TabsPrimitive.Content;

export function TabsList({ children }: { children: ReactNode }) {
  return <TabsPrimitive.List className="flex gap-1 border-b">{children}</TabsPrimitive.List>;
}

export function TabsTrigger({ value, children }: { value: string; children: ReactNode }) {
  return (
    <TabsPrimitive.Trigger
      value={value}
      className="-mb-px border-b-2 border-transparent px-3 py-2 text-sm text-muted hover:text-ink data-[state=active]:border-accent data-[state=active]:font-medium data-[state=active]:text-ink"
    >
      {children}
    </TabsPrimitive.Trigger>
  );
}
