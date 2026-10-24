import { cva, type VariantProps } from "class-variance-authority";
import type { ButtonHTMLAttributes } from "react";
import { cn } from "../../lib/utils";

const buttonVariants = cva(
  "ui-button focus-visible:outline-none",
  {
    variants: {
      variant: {
        default: "ui-button--primary",
        secondary: "ui-button--secondary",
        ghost: "ui-button--ghost",
        danger: "ui-button--danger",
      },
      size: { default: "h-9 px-4", sm: "h-8 px-3 text-xs", lg: "h-10 px-4" },
    },
    defaultVariants: { variant: "default", size: "default" },
  },
);

export interface ButtonProps extends ButtonHTMLAttributes<HTMLButtonElement>, VariantProps<typeof buttonVariants> {}

export function Button({ className, variant, size, ...props }: ButtonProps) {
  return <button className={cn(buttonVariants({ variant, size }), className)} {...props} />;
}
